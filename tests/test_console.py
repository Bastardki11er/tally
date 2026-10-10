"""How the console is served: the browser logic itself is tested in tests/js (node --test)."""

import re

from tally.api import STATIC_DIR


def test_the_console_page_forbids_anything_not_from_this_origin(client):
    response = client.get("/")

    assert response.status_code == 200
    policy = dict(part.strip().split(" ", 1) for part in response.headers["Content-Security-Policy"].split(";"))
    assert policy["script-src"] == "'self'"  # no inline script, no third-party script
    assert policy["frame-ancestors"] == "'none'"  # can't be framed for clickjacking


def test_the_page_and_its_files_are_revalidated_so_a_deploy_never_mixes_versions(client):
    for url in ("/", "/static/app.js", "/static/money.js", "/static/actions.js", "/static/styles.css"):
        response = client.get(url)
        assert response.status_code == 200, url
        assert response.headers["Cache-Control"] == "no-cache", url

    etag = client.get("/static/app.js").headers["ETag"]
    assert client.get("/static/app.js", headers={"If-None-Match": etag}).status_code == 304


def test_the_console_needs_nothing_the_policy_would_block():
    # The policy above would silently break the page if it ever grew inline code
    # or pulled from a CDN; catch that here instead of in someone's browser.
    for name in ("index.html", "app.js"):
        source = (STATIC_DIR / name).read_text()
        assert not re.search(r"<script(?![^>]*\bsrc=)", source), f"inline <script> in {name}"
        assert not re.search(r"\son[a-z]+\s*=\s*[\"']", source), f"inline event handler in {name}"
        assert not re.search(r"(src|href)=\"https?://(?!github\.com)", source), f"external resource in {name}"
