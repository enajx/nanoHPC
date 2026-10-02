"""Render the website's nginx template as the deploy does, for the main settings. nginx itself checks the
result on the simulated cluster (the deploy runs `nginx -t`)."""

import re
import unittest
from pathlib import Path
from typing import Any

import jinja2

TEMPLATE = Path(__file__).resolve().parents[1] / "src/nanohpc/ansible/roles/website/templates/website.conf.j2"


def render(website: dict[str, Any], certificate: str | None) -> str:
    """Render the template with Ansible's regex_escape filter (re.escape)."""
    environment = jinja2.Environment(trim_blocks=True, undefined=jinja2.StrictUndefined)
    environment.filters["regex_escape"] = re.escape
    template = environment.from_string(TEMPLATE.read_text())
    return template.render(
        website=website, tls_certificate=certificate, tls_certificate_key=None if certificate is None else "/k.pem"
    )


def website_of(changes: dict[str, Any]) -> dict[str, Any]:
    """Return website variables like the deploy's, with some changed."""
    website = {
        "hostname": "cluster.example.org",
        "path": "/cluster/",
        "allow": [],
        "forwarded_by": None,
        "logo": None,
    }
    website.update(changes)
    return website


class WebsiteNginxTest(unittest.TestCase):
    """Routes, access limits, and the path, as written into nginx's configuration."""

    def test_default_site(self) -> None:
        text = render(website_of({}), "/c.pem")
        self.assertIn("return 301 https://cluster.example.org$request_uri;", text)
        self.assertIn("location ^~ /cluster/ {", text)
        self.assertIn("alias /srv/nanohpc-web/current/;", text)
        self.assertIn("location = /cluster { return 301 /cluster/; }", text)
        self.assertIn("location = /cluster/data/status.json {", text)
        # The filled guide pages, not the templates in the build (which the site's prefix location serves).
        self.assertIn(
            "location = /cluster/docs.md {\n        limit_except GET { deny all; }\n        alias /srv/nanohpc-web/config/docs.md;",
            text,
        )
        self.assertIn("location = /cluster/policy.md {", text)
        self.assertIn("location ~ ^/cluster/grafana/api/(frontend/settings|", text)
        self.assertIn("nanohpc-queue-history", text)
        self.assertEqual(text.count("allow all;"), 4)
        self.assertNotIn("set_real_ip_from", text)
        self.assertIn("ssl_certificate /c.pem;", text)
        self.assertIn("location = /cluster/index.html {", text)
        self.assertIn("add_header Content-Security-Policy \"frame-ancestors 'self'\" always;", text)
        self.assertNotIn("allow 127.0.0.1;", text)

    def test_before_the_certificate_exists_only_port_80(self) -> None:
        text = render(website_of({}), None)
        self.assertIn("/.well-known/acme-challenge/", text)
        self.assertNotIn("listen 443", text)

    def test_allow_list_and_forwarding(self) -> None:
        text = render(website_of({"allow": ["10.0.0.0/8", "192.168.1.7"], "forwarded_by": "203.0.113.5"}), "/c.pem")
        self.assertIn("set_real_ip_from 203.0.113.5;\n    real_ip_header X-Forwarded-For;", text)
        # Once for the whole site, and again inside each Grafana route (which otherwise denies everything);
        # the front node itself always, for its own checks.
        for network in ("10.0.0.0/8", "192.168.1.7", "127.0.0.1", "::1"):
            self.assertEqual(text.count(f"allow {network};"), 5, network)
        self.assertNotIn("allow all;", text)
        grafana_routes = re.findall(r"location (?:~|=) \^?/cluster/grafana/[^{]*\{(.*?)\n        \}", text, re.DOTALL)
        self.assertEqual(len(grafana_routes), 4)
        for route in grafana_routes:
            self.assertIn("deny all;", route.split("limit_except")[0])

    def test_whole_hostname_and_logo(self) -> None:
        text = render(website_of({"path": "/", "logo": {"source": "/x/l.svg", "name": "logo.svg"}}), "/c.pem")
        self.assertIn("location ^~ / {", text)
        self.assertIn("location = /logo.svg {", text)
        self.assertNotIn("return 404", text)
        self.assertNotIn("return 302", text)


if __name__ == "__main__":
    unittest.main()
