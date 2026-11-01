"""Static panel checks and JavaScript syntax check (Node.js required)."""
from html.parser import HTMLParser
from pathlib import Path
import re
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]


class PanelParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = set()
        self.labels = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if "id" in values:
            assert values["id"] not in self.ids, f"Tekrarli ID: {values['id']}"
            self.ids.add(values["id"])
        if tag == "label" and "for" in values:
            self.labels.append(values["for"])
        assert not any(name.lower().startswith("on") for name, _ in attrs), "Inline event handler"
        if tag in {"script", "link"}:
            assert not values.get("src", values.get("href", "")).startswith(("http:", "https:", "//")), "Harici bagimlilik"


def check_page(html, name):
    parser = PanelParser()
    parser.feed(html)
    for label in parser.labels:
        assert label in parser.ids, f"Eksik label hedefi: {label}"
    for target in re.findall(r"byId\('([^']+)'\)", html):
        assert target in parser.ids, f"Eksik JS hedefi: {target}"
    for target in re.findall(r"getElementById\('([^']+)'\)", html):
        assert target in parser.ids, f"Eksik JS hedefi: {target}"
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
        assert sink not in html, f"Guvenli olmayan DOM yazimi: {sink}"
    scripts = re.findall(r"<script\b[^>]*>(.*?)</script>", html, re.S | re.I)
    assert scripts, "Inline JS yok"
    node = shutil.which("node")
    if not node:
        raise SystemExit("Node.js gerekli; JS syntax kontrolu calistirilamadi.")
    output = ROOT / ".pio/panel-tests"
    output.mkdir(parents=True, exist_ok=True)
    js = output / f"{name}.js"
    js.write_text("\n".join(scripts), encoding="utf-8")
    subprocess.run([node, "--check", str(js)], check=True, cwd=ROOT)
    print(f"{name}: {len(parser.ids)} unique IDs, labels/DOM bindings and JS syntax OK")


def main():
    check_page((ROOT / "data/index.html").read_text(encoding="utf-8"), "panel")
    source = (ROOT / "src/main.cpp").read_text(encoding="utf-8")
    match = re.search(r'BUZZER_TEST_HTML\[\].*?R"HTML\((.*?)\)HTML";', source, re.S)
    assert match, "Firmware ses testi sayfasi eksik"
    check_page(match.group(1), "buzzer-test")
    wifi = (ROOT / "include/WifiPage.h").read_text(encoding="utf-8")
    match = re.search(r'WIFI_HTML\[\].*?R"HTML\((.*?)\)HTML";', wifi, re.S)
    assert match, "Firmware Wi-Fi sayfasi eksik"
    check_page(match.group(1), "wifi")
    subprocess.run([shutil.which("node"), str(ROOT / "test/test_wifi_page.js")], check=True, cwd=ROOT)
    for route, handler in (("/api/wifi", "handleWifiSave"), ("/api/wifi/forget", "handleWifiForget")):
        assert f'g_server.on("{route}", HTTP_POST, {handler});' in source
        body = source.split(f"static void {handler}() {{", 1)[1].split("\n}", 1)[0]
        assert "!requireAuth() || !requireCsrf() || !bodyWithinLimit()" in body
    wifi_status = source.split("static void handleWifiStatus() {", 1)[1].split("\n}", 1)[0]
    assert "!requireAuth()" in wifi_status and "password" not in wifi_status
    assert "out.str(pdks::network::settings().ssid)" in wifi_status
    for route, handler in (("test", "handleBuzzerTest"), ("stop", "handleBuzzerStop")):
        assert f'g_server.on("/api/buzzer/{route}", HTTP_POST, {handler});' in source
        body = source.split(f"static void {handler}() {{", 1)[1].split("\n}", 1)[0]
        assert "!requireAuth() || !requireCsrf() || !bodyWithinLimit()" in body
        assert "!cfg::ENABLE_BUZZER" in body
    setup = source.split("void setup() {", 1)[1].split("void loop()", 1)[0]
    assert setup.index("console::begin(115200)") < setup.index("pinMode(cfg::PIN_BUZZER, OUTPUT)")
    assert "if (cfg::SD_SOFTSPI) pinMode(cfg::PIN_SD_SOFT_MISO, INPUT);" in setup
    for cpp in (ROOT / "src").glob("*.cpp"):
        assert not re.search(r"\bSerial\s*\.", cpp.read_text(encoding="utf-8")), f"Dogrudan UART erisimi: {cpp}"
    print("TX integration: UART release order, SD input, protected sound routes OK")


if __name__ == "__main__":
    main()
