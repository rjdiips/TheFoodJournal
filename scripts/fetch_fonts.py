"""Download the latin variable woff2 files for the Food Journal's three families."""
import re
import urllib.request
from pathlib import Path

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"
)
CSS_URL = (
    "https://fonts.googleapis.com/css2?"
    "family=DM+Sans:ital,wght@0,100..1000;1,100..1000"
    "&family=Fraunces:ital,opsz,wght@0,9..144,300..700;1,9..144,300..600"
    "&family=Manrope:wght@400..800&display=swap"
)
FAMILIES = {"DM Sans": "dm-sans", "Fraunces": "fraunces", "Manrope": "manrope"}
OUT = Path(__file__).resolve().parent.parent / "web" / "fonts"


def get(url):
    request = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(request) as response:
        return response.read()


css = get(CSS_URL).decode("utf-8")
faces = []
for block in css.split("/* ")[1:]:
    if "U+0000-00FF" not in block:
        continue
    family = re.search(r"font-family: '([^']+)'", block).group(1)
    if family not in FAMILIES:
        continue
    faces.append(
        {
            "family": family,
            "style": re.search(r"font-style: (\w+)", block).group(1),
            "weight": re.search(r"font-weight: ([^;]+);", block).group(1).strip(),
            "url": re.search(r"url\((https[^)]+)\)", block).group(1),
        }
    )

OUT.mkdir(parents=True, exist_ok=True)
rules = []
for face in faces:
    suffix = "" if face["style"] == "normal" else "-italic"
    name = f"{FAMILIES[face['family']]}{suffix}.woff2"
    data = get(face["url"])
    (OUT / name).write_bytes(data)
    print(f"{name:32} {len(data) / 1024:6.1f} KB  weight {face['weight']}  {face['style']}")
    rules.append(
        "@font-face {\n"
        f"  font-family: '{face['family']}';\n"
        f"  font-style: {face['style']};\n"
        f"  font-weight: {face['weight']};\n"
        "  font-display: swap;\n"
        f"  src: url('/assets/fonts/{name}') format('woff2');\n"
        "}\n"
    )

(OUT.parent / "css" / "food-journal" / "fonts.css").write_text(
    "/* Self-hosted copies of the Food Journal's three families, latin subset,\n"
    "   variable weights. Downloaded from Google Fonts (SIL OFL 1.1, see\n"
    "   /assets/fonts/OFL-*.txt). Regenerate with scripts/fetch_fonts.py. */\n\n"
    + "\n".join(rules),
    encoding="utf-8",
)

for slug in FAMILIES.values():
    licence = get(
        f"https://raw.githubusercontent.com/google/fonts/main/ofl/{slug.replace('-', '')}/OFL.txt"
    )
    (OUT / f"OFL-{slug}.txt").write_bytes(licence)
    print(f"OFL-{slug}.txt {len(licence)} bytes")
