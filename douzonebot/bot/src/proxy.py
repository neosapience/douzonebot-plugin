import httpx
import logging
from typing import Optional

logger = logging.getLogger(__name__)

class TunnelCDPProxy:
    """
    Utility to handle Cloudflare Tunnel CDP URL rewriting.
    Chrome's /json/version returns 'localhost', which must be rewritten 
    to the tunnel's public hostname for remote WebSocket connections to work.
    """
    def __init__(self, tunnel_url: str):
        self.tunnel_url = tunnel_url.rstrip('/')
        self.hostname = self.tunnel_url.replace('https://', '').replace('http://', '')

    async def get_fixed_websocket_url(self) -> str:
        """
        Fetches the debug info from the tunnel and returns the corrected wss:// URL.
        """
        try:
            # http2=False to avoid 421 Misdirected Request errors with some tunnel providers
            # Note: Don't override Host header when the tunnel requires a specific Host value
            async with httpx.AsyncClient(timeout=10.0, http2=False) as client:
                endpoint = f"{self.tunnel_url}/json/version"

                resp = await client.get(endpoint)
                resp.raise_for_status()
                
                data = resp.json()
                ws_url = data.get('webSocketDebuggerUrl')
                
                if not ws_url:
                    raise ValueError("No webSocketDebuggerUrl found in /json/version")
                
                # Rewrite: ws://localhost:9444/... -> wss://{tunnel-hostname}/...
                # Note: Cloudflare Tunnels are always over HTTPS (WSS)
                fixed_url = ws_url.replace('ws://localhost:9444', f'wss://{self.hostname}')
                
                # If it's already using a UUID path, ensure it's correct
                if 'ws://127.0.0.1:9444' in ws_url:
                    fixed_url = ws_url.replace('ws://127.0.0.1:9444', f'wss://{self.hostname}')
                
                logger.info(f"Rewrote WebSocket URL: {ws_url} -> {fixed_url}")
                return fixed_url
                
        except Exception as e:
            logger.error(f"Failed to fetch/rewrite CDP URL: {e}")
            raise

    def get_direct_websocket_url(self, browser_uuid: str) -> str:
        """
        Generates a direct wss:// URL using a known browser UUID.
        Bypasses the /json/version check.
        """
        return f"wss://{self.hostname}/devtools/browser/{browser_uuid}"
