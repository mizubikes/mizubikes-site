"""Tell IndexNow (Bing, Yandex, Seznam, Naver, ...) about new and changed pages.

Runs from .github/workflows/indexnow.yml after a push to main:
  - normally submits the URLs of content files added, changed or removed in the push
  - submits every URL in the live sitemap when this script or the workflow itself
    changed, or when the workflow is run by hand with "all" ticked
The key file lives at static/7f2435b99189273c3121c108010bcfb7.txt so it is served at https://mizubikes.com/7f2435b99189273c3121c108010bcfb7.txt.
"""
import json
import os
import re
import subprocess
import sys
import urllib.request

HOST = "mizubikes.com"
KEY = "7f2435b99189273c3121c108010bcfb7"
ENDPOINT = "https://api.indexnow.org/indexnow"
SITE = f"https://{HOST}"


def git(*args):
    return subprocess.run(["git", *args], capture_output=True, text=True, check=False).stdout


def front_matter_value(text, key):
    m = re.search(rf'^{key}:\s*["\']?(.*?)["\']?\s*$', text, re.M)
    return m.group(1).strip() if m else None


def url_for(path, text):
    """Map a content file to its public URL, the way Hugo does for this site."""
    if not path.startswith("content/") or not path.endswith(".md"):
        return None
    if re.search(r"^draft:\s*true", text, re.M):
        return None
    url = front_matter_value(text, "url")
    if url:
        url = "/" + url.strip("/") + "/"
        return SITE + (url if url != "//" else "/")
    rel = path[len("content/"):-len(".md")]
    parts = rel.split("/")
    if parts[-1] in ("index", "_index"):
        parts = parts[:-1]
    if not parts:
        return SITE + "/"
    slug = front_matter_value(text, "slug") or parts[-1]
    section = "/".join(parts[:-1])
    return f"{SITE}/{section + '/' if section else ''}{slug}/"


def changed_urls(before, after):
    urls = []
    diff = git("diff", "--name-status", f"{before}..{after}", "--", "content")
    for line in diff.splitlines():
        fields = line.split("\t")
        status, paths = fields[0], fields[1:]
        for path in paths:
            # removed or renamed-away files: read them from the old commit
            text = git("show", f"{after}:{path}") or git("show", f"{before}:{path}")
            u = url_for(path, text)
            if u:
                urls.append(u)
    return urls


def sitemap_urls():
    with urllib.request.urlopen(f"{SITE}/sitemap.xml", timeout=30) as r:
        xml = r.read().decode("utf-8", "replace")
    urls = re.findall(r"<loc>\s*(.*?)\s*</loc>", xml)
    # sitemap index -> fetch children
    if "<sitemapindex" in xml:
        out = []
        for child in urls:
            with urllib.request.urlopen(child, timeout=30) as r:
                out += re.findall(r"<loc>\s*(.*?)\s*</loc>", r.read().decode("utf-8", "replace"))
        urls = out
    return [u for u in urls if u.startswith(SITE) and "/tags/" not in u]


def submit(urls):
    body = json.dumps({
        "host": HOST,
        "key": KEY,
        "keyLocation": f"{SITE}/{KEY}.txt",
        "urlList": urls,
    }).encode()
    req = urllib.request.Request(ENDPOINT, data=body, method="POST",
                                 headers={"Content-Type": "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def main():
    before = os.environ.get("BEFORE", "")
    after = os.environ.get("AFTER", "HEAD")
    submit_all = os.environ.get("SUBMIT_ALL", "").lower() == "true"
    if before and not set(before) <= {"0"}:
        touched = git("diff", "--name-only", f"{before}..{after}")
        if "scripts/indexnow.py" in touched or ".github/workflows/indexnow.yml" in touched:
            submit_all = True
    else:
        submit_all = True  # first push on a branch or a manual run: no "before" to diff

    urls = sitemap_urls() if submit_all else changed_urls(before, after)
    urls = list(dict.fromkeys(urls))[:10000]
    if not urls:
        print("No content URLs changed; nothing to submit.")
        return
    status = submit(urls)
    print(f"Submitted {len(urls)} URL(s) to IndexNow: HTTP {status}")
    for u in urls[:50]:
        print("  ", u)
    if status not in (200, 202):
        sys.exit(f"IndexNow rejected the submission (HTTP {status}).")


if __name__ == "__main__":
    main()
