"""Redirect policy for downloads of images from external services.

urllib follows redirects by default and does not look at the new host. Every hop is therefore checked against an
allow-list before it is followed, and a refused hop raises APIError before any request is sent to it.
"""
import re
import urllib.request

from meloshelf.infrastructure.music_api import APIError

# Wikimedia Commons file storage (upload, thumbnail and the Commons site itself), over https only.
WIKIMEDIA_IMAGE_HOST = re.compile(r'^https://(?:upload|thumb|commons)\.wikimedia\.org/')
# Cover Art Archive redirects its images to the Internet Archive's storage hosts.
ARCHIVE_IMAGE_HOST = re.compile(r'^https://(?:[a-z0-9-]+\.)*(?:coverartarchive\.org|archive\.org)/')


class HostBoundRedirects(urllib.request.HTTPRedirectHandler):
    """Follows a redirect only when its target matches the allow-list."""

    def __init__(self, allowed: re.Pattern):
        self.allowed = allowed

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not self.allowed.match(newurl or ''):
            raise APIError('Refusing to follow a redirect to a host that is not allowed', retryable=False)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def open_restricted(request, allowed: re.Pattern, timeout: float = 20):
    """urlopen with redirects restricted to ``allowed``."""
    opener = urllib.request.build_opener(HostBoundRedirects(allowed))
    return opener.open(request, timeout=timeout)
