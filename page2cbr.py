import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from html import unescape
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup


IMG_EXT_RE = re.compile(r"\.(jpe?g|png|gif|webp|bmp|tiff?)(\?|#|$)", re.IGNORECASE)


def natural_key(s: str):
    # Natural sort: "10" > "2"
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def is_probably_image_url(u: str) -> bool:
    return bool(IMG_EXT_RE.search(u))


def clean_url(u: str) -> str:
    u = unescape(u).strip()
    # Remove surrounding quotes if present
    if (u.startswith('"') and u.endswith('"')) or (u.startswith("'") and u.endswith("'")):
        u = u[1:-1]
    return u.strip()


def parse_srcset(srcset: str):
    # Returns list of URLs from srcset
    urls = []
    for part in srcset.split(","):
        part = part.strip()
        if not part:
            continue
        # format: "url 2x" or "url 1200w"
        url = part.split()[0].strip()
        if url:
            urls.append(url)
    return urls


def extract_image_urls_from_html(base_url: str, html: str):
    soup = BeautifulSoup(html, "lxml")
    urls = []

    # 1) <img ...>
    for img in soup.find_all("img"):
        for attr in ("src", "data-src", "data-original", "data-lazy", "data-img", "data-image"):
            v = img.get(attr)
            if v:
                urls.append(v)
        srcset = img.get("srcset") or img.get("data-srcset")
        if srcset:
            urls.extend(parse_srcset(srcset))

    # 2) <a href="...jpg">
    for a in soup.find_all("a"):
        href = a.get("href")
        if href and is_probably_image_url(href):
            urls.append(href)

    # 3) Search raw text for image-like URLs (last resort)
    #    Useful for pages that embed links in scripts.
    raw_candidates = set(re.findall(r"https?://[^\s'\"<>]+", html))
    for c in raw_candidates:
        if is_probably_image_url(c):
            urls.append(c)

    # Normalize, absolutize, dedupe
    norm = []
    seen = set()
    for u in urls:
        u = clean_url(u)
        if not u:
            continue
        abs_u = urljoin(base_url, u)
        if abs_u not in seen:
            seen.add(abs_u)
            norm.append(abs_u)

    # Keep only those that look like images (heuristic)
    # Note: Some sites serve images from extension-less URLs; you can disable this filter with --no-ext-filter
    return norm


def ensure_dir(p: str):
    os.makedirs(p, exist_ok=True)


@dataclass
class DownloadResult:
    ok: bool
    path: str
    url: str
    reason: str = ""


def download_one(session: requests.Session, url: str, out_path: str, referer: str | None, timeout: int):
    headers = {}
    if referer:
        headers["Referer"] = referer

    try:
        r = session.get(url, headers=headers, stream=True, timeout=timeout)
        r.raise_for_status()
        ctype = (r.headers.get("Content-Type") or "").lower()
        if "image" not in ctype and not is_probably_image_url(url):
            return DownloadResult(False, out_path, url, f"Content-Type no parece imagen: {ctype}")

        with open(out_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 128):
                if chunk:
                    f.write(chunk)

        if os.path.getsize(out_path) < 1024:  # muy pequeño = probablemente icono o error
            return DownloadResult(False, out_path, url, "Archivo demasiado pequeño (posible error/icono)")

        return DownloadResult(True, out_path, url)

    except requests.RequestException as e:
        return DownloadResult(False, out_path, url, f"HTTP error: {e}")
    except OSError as e:
        return DownloadResult(False, out_path, url, f"FS error: {e}")


def find_rar_executable(explicit: str | None):
    if explicit:
        return explicit
    # Try common names
    for cmd in ("rar", "rar.exe"):
        if shutil.which(cmd):
            return cmd
    return None


def make_cbr_with_rar(rar_cmd: str, images_dir: str, out_cbr: str):
    # Create RAR in current working directory; use -ep (no path), -idq (quiet)
    # We rely on the order being set by filename: 001.jpg, 002.jpg, ...
    cwd = images_dir
    files = sorted([f for f in os.listdir(images_dir) if os.path.isfile(os.path.join(images_dir, f))], key=natural_key)
    if not files:
        raise RuntimeError("No hay imágenes para empaquetar.")

    # rar a -ep -idq out.cbr 001.jpg 002.jpg ...
    cmd = [rar_cmd, "a", "-ep", "-idq", out_cbr] + files
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"RAR falló: {p.stderr.strip() or p.stdout.strip()}")


def make_cbz_zip(images_dir: str, out_cbz: str):
    import zipfile

    files = sorted([f for f in os.listdir(images_dir) if os.path.isfile(os.path.join(images_dir, f))], key=natural_key)
    if not files:
        raise RuntimeError("No hay imágenes para empaquetar.")

    with zipfile.ZipFile(out_cbz, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for f in files:
            z.write(os.path.join(images_dir, f), arcname=f)


async def fetch_html_playwright(url: str, wait_ms: int, user_agent: str | None):
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(user_agent=user_agent) if user_agent else await browser.new_context()
        page = await context.new_page()
        await page.goto(url, wait_until="networkidle")
        if wait_ms > 0:
            await page.wait_for_timeout(wait_ms)
        html = await page.content()
        await browser.close()
        return html


def main():
    ap = argparse.ArgumentParser(
        description="Descarga imágenes de una página y genera un CBR (RAR) o CBZ (ZIP fallback)."
    )
    ap.add_argument("url", help="URL de la página a extraer")
    ap.add_argument("-o", "--output", default="comic", help="Nombre base de salida (sin extensión)")
    ap.add_argument("--timeout", type=int, default=30, help="Timeout HTTP por request (segundos)")
    ap.add_argument("--user-agent", default="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome Safari",
                    help="User-Agent")
    ap.add_argument("--referer", default=None, help="Referer HTTP (si la web lo requiere)")
    ap.add_argument("--cookie", default=None, help='Cookie header cruda, ej: "session=...; other=..." (solo si corresponde)')
    ap.add_argument("--max-images", type=int, default=0, help="Limitar cantidad de imágenes (0 = sin límite)")
    ap.add_argument("--no-ext-filter", action="store_true", help="No filtrar por extensión (para URLs sin .jpg/.png)")
    ap.add_argument("--rar", default=None, help="Ruta explícita a rar.exe/rar (si no está en PATH)")

    # Playwright (para páginas dinámicas)
    ap.add_argument("--playwright", action="store_true", help="Usar Playwright para obtener HTML (páginas con JS)")
    ap.add_argument("--wait-ms", type=int, default=0, help="Espera extra (ms) después de cargar (solo Playwright)")

    args = ap.parse_args()

    url = args.url
    out_base = args.output

    session = requests.Session()
    session.headers.update({"User-Agent": args.user_agent})
    if args.cookie:
        session.headers.update({"Cookie": args.cookie})

    # 1) Obtener HTML
    if args.playwright:
        try:
            import asyncio
            html = asyncio.run(fetch_html_playwright(url, args.wait_ms, args.user_agent))
        except Exception as e:
            print(f"[ERROR] Playwright falló: {e}", file=sys.stderr)
            sys.exit(2)
    else:
        try:
            r = session.get(url, timeout=args.timeout)
            r.raise_for_status()
            html = r.text
        except requests.RequestException as e:
            print(f"[ERROR] No pude obtener la página: {e}", file=sys.stderr)
            sys.exit(2)

    base_url = url

    # 2) Extraer URLs de imágenes
    img_urls = extract_image_urls_from_html(base_url, html)

    if not args.no_ext_filter:
        img_urls = [u for u in img_urls if is_probably_image_url(u)]

    if args.max_images and args.max_images > 0:
        img_urls = img_urls[: args.max_images]

    if not img_urls:
        print("[ERROR] No encontré URLs de imágenes en la página.")
        print("Sugerencias: prueba --playwright, o usa --no-ext-filter si las imágenes no terminan en .jpg/.png.")
        sys.exit(3)

    # Ordenar de forma estable (natural sort por URL)
    img_urls = sorted(img_urls, key=natural_key)

    # 3) Descargar
    with tempfile.TemporaryDirectory(prefix="page2cbr_") as tmpdir:
        images_dir = os.path.join(tmpdir, "images")
        ensure_dir(images_dir)

        ok_count = 0
        failures = []

        for idx, u in enumerate(img_urls, start=1):
            ext = os.path.splitext(urlparse(u).path)[1].lower()
            if not ext or len(ext) > 6:
                ext = ".jpg"  # fallback
            fname = f"{idx:04d}{ext}"
            out_path = os.path.join(images_dir, fname)

            res = download_one(session, u, out_path, args.referer or url, args.timeout)
            if res.ok:
                ok_count += 1
                print(f"[OK] {idx}/{len(img_urls)} {u}")
            else:
                failures.append((u, res.reason))
                # borrar parcial si existe
                try:
                    if os.path.exists(out_path):
                        os.remove(out_path)
                except OSError:
                    pass
                print(f"[SKIP] {idx}/{len(img_urls)} {u} -> {res.reason}")

        if ok_count == 0:
            print("[ERROR] No se descargó ninguna imagen utilizable.", file=sys.stderr)
            sys.exit(4)

        # 4) Empaquetar
        rar_cmd = find_rar_executable(args.rar)

        if rar_cmd:
            out_cbr = f"{out_base}.cbr"
            try:
                # Crear en dir actual
                make_cbr_with_rar(rar_cmd, images_dir, os.path.abspath(out_cbr))
                print(f"[DONE] Generado: {out_cbr}")
            except Exception as e:
                print(f"[WARN] No pude crear CBR con rar ({e}). Haré CBZ.", file=sys.stderr)
                out_cbz = f"{out_base}.cbz"
                make_cbz_zip(images_dir, os.path.abspath(out_cbz))
                print(f"[DONE] Generado: {out_cbz}")
        else:
            out_cbz = f"{out_base}.cbz"
            make_cbz_zip(images_dir, os.path.abspath(out_cbz))
            print(f"[DONE] rar no encontrado. Generado: {out_cbz}")

        if failures:
            print("\n[INFO] Algunas imágenes fallaron o se omitieron:")
            for u, reason in failures[:20]:
                print(f" - {u} | {reason}")
            if len(failures) > 20:
                print(f" - ... ({len(failures)-20} más)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
