import io
import os
import secrets
import string
import sqlite3
from urllib.parse import urlparse

import qrcode
from flask import Flask, abort, jsonify, redirect, render_template, request, send_from_directory

app = Flask(__name__)

DB_HOST = os.environ.get("DB_HOST")
DB_USER = os.environ.get("DB_USER", "admin")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "")
DB_NAME = os.environ.get("DB_NAME", "shortener")
S3_BUCKET = os.environ.get("S3_BUCKET")
AWS_REGION = os.environ.get("AWS_REGION", "ap-southeast-2")
BASE_URL = os.environ.get("BASE_URL", "").rstrip("/")

USE_AWS = bool(DB_HOST and S3_BUCKET)
ALPHABET = string.ascii_letters + string.digits
LOCAL_QR_DIR = os.path.join(os.path.dirname(__file__), "local_qr")
SQLITE_PATH = os.path.join(os.path.dirname(__file__), "local.db")
PH = "%s" if USE_AWS else "?"


def get_conn():
    if USE_AWS:
        import pymysql
        return pymysql.connect(host=DB_HOST, user=DB_USER, password=DB_PASSWORD,
                               database=DB_NAME, autocommit=True, connect_timeout=5)
    return sqlite3.connect(SQLITE_PATH)


def init_db():
    conn = get_conn()
    cur = conn.cursor()
    if USE_AWS:
        cur.execute("""CREATE TABLE IF NOT EXISTS links (
            id INT AUTO_INCREMENT PRIMARY KEY,
            code VARCHAR(12) NOT NULL UNIQUE,
            original_url TEXT NOT NULL,
            clicks INT NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    else:
        cur.execute("""CREATE TABLE IF NOT EXISTS links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL UNIQUE,
            original_url TEXT NOT NULL,
            clicks INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    conn.commit()
    conn.close()


def query(sql, params=(), fetch=None, commit=False):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        result = None
        if fetch == "one":
            result = cur.fetchone()
        elif fetch == "all":
            result = cur.fetchall()
        if commit:
            conn.commit()
        return result
    finally:
        conn.close()


def make_qr_png(data):
    img = qrcode.make(data)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def save_qr(code, png):
    if USE_AWS:
        import boto3
        s3 = boto3.client("s3", region_name=AWS_REGION)
        s3.put_object(Bucket=S3_BUCKET, Key=f"qr/{code}.png", Body=png, ContentType="image/png")
    else:
        os.makedirs(LOCAL_QR_DIR, exist_ok=True)
        with open(os.path.join(LOCAL_QR_DIR, f"{code}.png"), "wb") as f:
            f.write(png)


def qr_url(code):
    if USE_AWS:
        import boto3
        s3 = boto3.client("s3", region_name=AWS_REGION)
        return s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": S3_BUCKET, "Key": f"qr/{code}.png"},
            ExpiresIn=3600)
    return f"/qr/{code}.png"


def valid_url(url):
    try:
        p = urlparse(url)
        return p.scheme in ("http", "https") and bool(p.netloc)
    except ValueError:
        return False


def new_code(length=6):
    for _ in range(10):
        code = "".join(secrets.choice(ALPHABET) for _ in range(length))
        if not query(f"SELECT 1 FROM links WHERE code = {PH}", (code,), fetch="one"):
            return code
    return new_code(length + 1)


def base_url():
    return BASE_URL or request.host_url.rstrip("/")


@app.route("/")
def index():
    rows = query("SELECT code, original_url, clicks FROM links ORDER BY id DESC LIMIT 10", fetch="all")
    links = [{"code": r[0], "url": r[1], "clicks": r[2],
              "short": f"{base_url()}/{r[0]}", "qr": qr_url(r[0])} for r in rows]
    return render_template("index.html", links=links)


@app.route("/shorten", methods=["POST"])
def shorten():
    data = request.get_json(silent=True) or request.form
    url = (data.get("url") or "").strip()
    if not valid_url(url):
        return jsonify(error="Please enter a valid http(s) URL"), 400
    code = new_code()
    query(f"INSERT INTO links (code, original_url) VALUES ({PH}, {PH})", (code, url), commit=True)
    short = f"{base_url()}/{code}"
    save_qr(code, make_qr_png(short))
    return jsonify(code=code, short_url=short, qr_url=qr_url(code), original_url=url), 201


@app.route("/health")
def health():
    return jsonify(status="ok", mode="aws" if USE_AWS else "local")


@app.route("/qr/<code>.png")
def local_qr(code):
    if USE_AWS:
        abort(404)
    return send_from_directory(LOCAL_QR_DIR, f"{code}.png")


@app.route("/stats/<code>")
def stats(code):
    row = query(f"SELECT original_url, clicks FROM links WHERE code = {PH}", (code,), fetch="one")
    if not row:
        abort(404)
    return jsonify(code=code, original_url=row[0], clicks=row[1])


@app.route("/<code>")
def go(code):
    row = query(f"SELECT original_url FROM links WHERE code = {PH}", (code,), fetch="one")
    if not row:
        abort(404)
    query(f"UPDATE links SET clicks = clicks + 1 WHERE code = {PH}", (code,), commit=True)
    return redirect(row[0], code=302)


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=not USE_AWS)