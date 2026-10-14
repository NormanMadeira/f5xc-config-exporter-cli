import logging
import time
from urllib.parse import quote
import requests

LOG = logging.getLogger("f5xc.api")

class XCAPIError(RuntimeError):
    def __init__(self, message, status_code=None, url=None):
        super().__init__(message)
        self.status_code = status_code
        self.url = url

class XCClient:
    def __init__(self, tenant_url, token, timeout=45, retries=3, verify_tls=True):
        tenant_url = tenant_url.strip().rstrip("/")
        if not tenant_url.startswith(("https://", "http://")):
            tenant_url = "https://" + tenant_url
        self.base_url = tenant_url
        self.timeout, self.retries, self.verify_tls = timeout, retries, verify_tls
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"APIToken {token.strip()}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "F5-XC-Config-Exporter-CLI/1.0",
        })
        LOG.debug("Client initialized host=%s timeout=%s retries=%s verify_tls=%s",
                  self.base_url, timeout, retries, verify_tls)

    @staticmethod
    def q(value):
        return quote(str(value), safe="")

    def request_json(self, method, path, params=None):
        url = self.base_url + path
        LOG.debug("HTTP request start method=%s url=%s (authorization header omitted)", method, url)
        for attempt in range(self.retries + 1):
            started = time.monotonic()
            try:
                response = self.session.request(
                    method, url, params=params, timeout=self.timeout, verify=self.verify_tls
                )
                elapsed = time.monotonic() - started
                LOG.info("HTTP response status=%s elapsed=%.3fs url=%s attempt=%s/%s",
                         response.status_code, elapsed, response.url, attempt + 1, self.retries + 1)
                if (response.status_code == 429 or 500 <= response.status_code < 600) and attempt < self.retries:
                    retry_after = response.headers.get("Retry-After")
                    try:
                        delay = min(30, max(1, int(retry_after))) if retry_after else min(12, 2 ** attempt)
                    except ValueError:
                        delay = min(12, 2 ** attempt)
                    LOG.warning("Transient response; retrying in %ss", delay)
                    time.sleep(delay)
                    continue
                if not response.ok:
                    body = response.text[:1500]
                    if response.status_code == 404:
                        LOG.warning("API resource not found status=404 url=%s body=%s", response.url, body)
                    else:
                        LOG.error("API error status=%s url=%s body=%s", response.status_code, response.url, body)
                    raise XCAPIError(f"HTTP {response.status_code}: {body}", response.status_code, response.url)
                if not response.content:
                    return {}
                try:
                    data = response.json()
                except ValueError as exc:
                    raise XCAPIError("API returned non-JSON content", response.status_code, response.url) from exc
                LOG.debug("Parsed JSON type=%s response_bytes=%s", type(data).__name__, len(response.content))
                return data
            except XCAPIError:
                raise
            except requests.RequestException as exc:
                LOG.warning("Request exception attempt=%s/%s: %s", attempt + 1, self.retries + 1, exc)
                if attempt < self.retries:
                    delay = min(12, 2 ** attempt)
                    LOG.warning("Retrying in %ss", delay)
                    time.sleep(delay)
                else:
                    raise XCAPIError(f"Request failed after retries: {exc}", url=url) from exc
        raise XCAPIError("Request failed for an unknown reason", url=url)

    def list_collection(self, namespace, collection, api_root="/api/config/namespaces"):
        return self.request_json("GET", f"{api_root}/{self.q(namespace)}/{self.q(collection)}")

    def get_object(self, namespace, collection, name, api_root="/api/config/namespaces", response_format=None):
        path = f"{api_root}/{self.q(namespace)}/{self.q(collection)}/{self.q(name)}"
        params = {"response_format": response_format} if response_format else None
        return self.request_json("GET", path, params=params)
