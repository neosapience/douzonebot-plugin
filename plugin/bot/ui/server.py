"""
Douzone Bot Web Dashboard — Flask Backend.

Serves the web dashboard at localhost:5000 for data input.
Provides REST API for the frontend (memo + receipt upload).
Claude agent handles the actual pipeline execution via CLI.
"""
import os
import sys
import json
import logging
import shutil
from pathlib import Path
from flask import Flask, render_template, request, jsonify

# Add project root to sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.config import load_config, AppConfig
from src.llm_provider import create_provider, LLMProvider

VERSION = "0.3.0"

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Supported image extensions for receipt scanning
IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.heic', '.heif', '.bmp', '.tiff', '.tif'}

_config: AppConfig = None
_provider: LLMProvider = None

TEMP_UPLOAD_DIR = Path(os.path.join(os.path.dirname(__file__), "temp_uploads"))
TEMP_UPLOAD_DIR.mkdir(exist_ok=True)
(TEMP_UPLOAD_DIR / "receipts").mkdir(exist_ok=True)


def _init_config():
    """Load config and create LLM provider on first use."""
    global _config, _provider
    if _config is None:
        _config = load_config()
        _config.mode = "local"
    if _provider is None:
        _provider = create_provider(_config, provider_type="llm")
    return _config, _provider


def _error_response(code: str, message: str, fix: str = None, status_code: int = 400):
    """Return a structured error response."""
    resp = {"status": "error", "code": code, "message": message}
    if fix:
        resp["fix"] = fix
    return jsonify(resp), status_code


# ============================================================================
# CORE ROUTES
# ============================================================================

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/health')
def health():
    """Health check endpoint for plugin integration."""
    return jsonify({"status": "ok", "version": VERSION, "app": "douzone-bot"})


@app.route('/shutdown', methods=['POST'])
def shutdown():
    """Gracefully stop the dashboard server."""
    func = request.environ.get('werkzeug.server.shutdown')
    if func is not None:
        func()
    else:
        # Fallback: signal-based shutdown (works with all WSGI servers)
        import signal
        os.kill(os.getpid(), signal.SIGTERM)
    return jsonify({"status": "shutting_down"})


@app.route('/preflight')
def preflight():
    """Run preflight checks and return status for UI connection indicator."""
    from src.preflight import check_cdp, check_claude_cli

    config, _ = _init_config()
    cdp_url = request.args.get('cdp_url', f'http://localhost:{config.chrome_debug_port}')

    results = {}

    # Always check CDP
    ok, msg = check_cdp(cdp_url)
    results['cdp'] = {'available': ok, 'message': msg}

    # Check Claude Code CLI
    ok, msg = check_claude_cli()
    results['llm'] = {'available': ok, 'message': msg, 'provider': 'claude_code'}

    all_ok = all(r['available'] for r in results.values())

    return jsonify({
        'status': 'ok' if all_ok else 'warning',
        'checks': results,
    })


@app.route('/config')
def get_config():
    """Return current configuration for frontend pre-fill."""
    config, provider = _init_config()
    return jsonify({
        'user_name': config.user_name,
        'mode': config.mode,
        'llm_provider': config.llm_provider,
        'receipt_provider': config.receipt_provider,
        'provider_name': provider.name,
        'chrome_debug_port': config.chrome_debug_port,
        'cdp_url': f'http://localhost:{config.chrome_debug_port}',
    })


# ============================================================================
# FILE BROWSING
# ============================================================================

@app.route('/browse')
def browse_directory():
    """Browse filesystem directories for folder/file selection.

    Query params:
        path: Directory path to browse (default: ~)
        mode: 'dirs' (directories only) or 'all' (dirs + files)

    This is a localhost-only server, so no path restriction is needed.
    """
    raw_path = request.args.get('path', '~')
    mode = request.args.get('mode', 'dirs')

    # Resolve path
    try:
        resolved = Path(raw_path).expanduser().resolve()
    except Exception:
        return _error_response("INVALID_PATH", f"Invalid path: {raw_path}")

    if not resolved.is_dir():
        return _error_response("NOT_A_DIRECTORY", f"Not a directory: {raw_path}")

    # List contents
    dirs = []
    files = []
    try:
        for item in sorted(resolved.iterdir()):
            # Skip hidden files/dirs
            if item.name.startswith('.'):
                continue
            try:
                if item.is_dir():
                    dirs.append(item.name)
                elif mode == 'all' and item.is_file():
                    files.append(item.name)
            except (PermissionError, OSError):
                continue  # Skip inaccessible items
    except PermissionError:
        return _error_response("PERMISSION_DENIED", f"Cannot read directory: {raw_path}")

    # Compute display path (normalize to forward slashes for cross-platform JS safety)
    home = Path.home().resolve()
    display_path = str(resolved).replace(str(home), '~', 1).replace('\\', '/')
    parent = resolved.parent
    parent_path = str(parent).replace(str(home), '~', 1).replace('\\', '/')

    # No parent link if we're at a filesystem root (/, C:\, etc.)
    has_parent = resolved != parent

    return jsonify({
        'current': display_path,
        'current_absolute': str(resolved),
        'parent': parent_path if has_parent else None,
        'dirs': dirs,
        'files': files,
    })


# ============================================================================
# PATH VALIDATION
# ============================================================================

@app.route('/validate-path')
def validate_path():
    """Validate a folder path and count receipt images.

    Query params:
        path: Directory path to validate

    Returns image count, pre-OCR count, and validation status.
    """
    raw_path = request.args.get('path', '').strip()
    if not raw_path:
        return jsonify({'valid': False, 'reason': 'empty'})

    try:
        resolved = Path(raw_path).expanduser().resolve()
    except Exception:
        return jsonify({'valid': False, 'reason': 'invalid_path',
                        'message': f'경로를 해석할 수 없습니다: {raw_path}'})

    if not resolved.exists():
        return jsonify({'valid': False, 'reason': 'not_found',
                        'message': '폴더를 찾을 수 없습니다'})

    if not resolved.is_dir():
        return jsonify({'valid': False, 'reason': 'not_directory',
                        'message': '파일입니다 (폴더가 아님)'})

    # Count images and pre-OCR companion files
    image_count = 0
    preocr_count = 0
    preocr_extensions = {'.ocr.md', '.ocr.txt', '.ocr.json'}
    try:
        for f in resolved.iterdir():
            if not f.is_file():
                continue
            name_lower = f.name.lower()
            if f.suffix.lower() in IMAGE_EXTENSIONS:
                image_count += 1
            elif any(name_lower.endswith(ext) for ext in preocr_extensions):
                preocr_count += 1
    except PermissionError:
        return jsonify({'valid': False, 'reason': 'permission_denied',
                        'message': '폴더를 읽을 수 없습니다 (권한 부족)'})

    if image_count == 0:
        return jsonify({'valid': True, 'reason': 'no_images',
                        'message': '이미지 파일이 없습니다',
                        'path': str(resolved), 'images': 0, 'preocr': preocr_count})

    return jsonify({'valid': True, 'reason': 'ok',
                    'path': str(resolved), 'images': image_count,
                    'preocr': preocr_count})


# ============================================================================
# UPLOAD (save data for Claude agent to use)
# ============================================================================

@app.route('/upload', methods=['POST'])
def upload_files():
    """Save memo and receipts for the Claude agent pipeline."""
    try:
        user_name = request.form.get('user_name', '').strip()
        cdp_url = request.form.get('cdp_url', 'http://localhost:9444')

        # Save Memo (from textarea)
        memo_text = request.form.get('memo_text', '').strip()
        memo_path = None
        if memo_text:
            memo_path = TEMP_UPLOAD_DIR / "memo.txt"
            memo_path.write_text(memo_text, encoding='utf-8')

        # Determine receipt source: folder path OR uploaded files
        receipt_folder_path = request.form.get('receipt_folder_path', '').strip()
        receipts_dir = None
        has_receipts = False
        receipt_count = 0

        if receipt_folder_path:
            # Folder path mode: use the directory directly
            try:
                resolved = Path(receipt_folder_path).expanduser().resolve()
            except Exception:
                return jsonify({
                    "status": "error",
                    "message": f"영수증 폴더 경로를 해석할 수 없습니다: {receipt_folder_path}",
                })
            if not resolved.is_dir():
                return jsonify({
                    "status": "error",
                    "message": f"영수증 폴더를 찾을 수 없습니다: {receipt_folder_path}",
                })
            receipt_count = sum(1 for f in resolved.iterdir()
                               if f.is_file() and f.suffix.lower() in IMAGE_EXTENSIONS)
            if receipt_count > 0:
                receipts_dir = resolved
                has_receipts = True
                logger.info(f"Using receipt folder path: {resolved} ({receipt_count} images)")
            else:
                return jsonify({
                    "status": "error",
                    "message": f"폴더에 이미지 파일이 없습니다: {receipt_folder_path}",
                })

        if not has_receipts:
            # File upload mode: save uploaded files
            receipt_files = request.files.getlist('receipt_files')
            upload_receipts_dir = TEMP_UPLOAD_DIR / "receipts"
            # Clean previous receipts
            for f in upload_receipts_dir.glob("*"):
                if f.is_dir():
                    shutil.rmtree(f)
                else:
                    f.unlink()

            preocr_extensions = {'.ocr.md', '.ocr.txt', '.ocr.json'}
            ocr_count = 0
            if receipt_files:
                for rf in receipt_files:
                    if rf.filename:
                        rf.save(upload_receipts_dir / rf.filename)
                        name_lower = rf.filename.lower()
                        if any(name_lower.endswith(ext) for ext in preocr_extensions):
                            ocr_count += 1
                        elif Path(rf.filename).suffix.lower() in IMAGE_EXTENSIONS:
                            has_receipts = True
                            receipt_count += 1

            if has_receipts:
                receipts_dir = upload_receipts_dir

        # Save metadata for Claude agent to read
        metadata = {
            "user_name": user_name,
            "cdp_url": cdp_url,
            "memo_path": str(memo_path) if memo_path else None,
            "receipts_path": str(receipts_dir) if has_receipts else None,
            "receipt_count": receipt_count,
            "has_memo": bool(memo_text),
        }
        metadata_path = TEMP_UPLOAD_DIR / "session.json"
        metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding='utf-8')

        return jsonify({
            "status": "success",
            "message": "데이터가 저장되었습니다.",
            "memo": bool(memo_text),
            "receipts": receipt_count,
        })

    except Exception as e:
        logger.exception("Upload failed")
        return _error_response("UPLOAD_FAILED", str(e), status_code=500)


@app.route('/data')
def get_data():
    """Return saved session data for Claude agent to read."""
    metadata_path = TEMP_UPLOAD_DIR / "session.json"
    if not metadata_path.exists():
        return _error_response("NO_DATA", "No data saved yet. Use the dashboard to input data.")

    metadata = json.loads(metadata_path.read_text(encoding='utf-8'))
    return jsonify(metadata)


# ============================================================================
# MAIN
# ============================================================================

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=5000, help='Port to listen on')
    args = parser.parse_args()
    app.run(host='0.0.0.0', port=args.port, debug=True)
