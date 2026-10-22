"""D-owned pages for local single-module preview; the shared gateway still serves production pages."""
from pathlib import Path

from fastapi import HTTPException
from starlette.responses import FileResponse

FRONTEND_ROOT = Path(__file__).resolve().parents[2] / 'frontend'
FILES = {
    '/': ('index.html', 'text/html'),
    '/index.html': ('index.html', 'text/html'),
    '/login.html': ('login.html', 'text/html'),
    '/shared/auth.js': ('shared/auth.js', 'text/javascript'),
    '/shared/d-session.js': ('shared/d-session.js', 'text/javascript'),
    '/shared/d-pages.js': ('shared/d-pages.js', 'text/javascript'),
    '/shared/d.css': ('shared/d.css', 'text/css'),
}


def install_frontend(app):
    def endpoint_for(relative, media_type):
        async def endpoint():
            path = FRONTEND_ROOT / relative
            # Backend-only containers do not bundle HTML; their gateway owns static files.
            if not path.is_file():
                raise HTTPException(status_code=404)
            return FileResponse(path, media_type=media_type, headers={
                'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
            })
        return endpoint

    for url, (relative, media_type) in FILES.items():
        app.add_api_route(url, endpoint_for(relative, media_type), methods=['GET', 'HEAD'],
                          include_in_schema=False)
