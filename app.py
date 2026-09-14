from flask import Flask, jsonify, send_from_directory, request
import requests
import os

app = Flask(__name__, static_folder=".", static_url_path="")

FPL_BASE = "https://fantasy.premierleague.com/api/"
TIMEOUT = 15

@app.get("/")
def index():
    return send_from_directory(".", "index.html")

@app.get("/api/<path:path>")
def fpl_proxy(path):
    # Only proxy read-only GET requests to the public FPL API.
    # This deliberately does not forward cookies or authorization headers.
    if not path:
        return jsonify({"error": "Missing FPL API path"}), 400

    # Keep the proxy restricted to the FPL API path space.
    url = FPL_BASE + path
    try:
        r = requests.get(
            url,
            params=request.args,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; SajedFantasy/1.0)",
                "Accept": "application/json",
            },
            timeout=TIMEOUT,
        )
    except requests.RequestException as exc:
        return jsonify({"error": "FPL request failed", "detail": str(exc)}), 502

    content_type = r.headers.get("Content-Type", "application/json")
    return (r.content, r.status_code, {"Content-Type": content_type, "Cache-Control": "no-store"})

@app.get("/health")
def health():
    return jsonify({"ok": True})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
