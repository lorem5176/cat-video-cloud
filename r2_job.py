#!/usr/bin/env python3
"""
Cloud job: pull source files from Cloudflare R2 -> render videos -> push results to R2.

Bucket layout (create these "folders" by uploading files with these prefixes):
    Audio/...                 audio tracks
    Assets/Hooks/...          hook clips
    Assets/Cats/...           cats clips
    Assets/Eye/...            eye clips
    Assets/Sad/...            sad clips
    output/<timestamp>/N.mp4  <- results are written here

Required environment variables:
    R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET

Where finished videos go (env UPLOAD_TO):
    r2      -> R2 bucket under output/<timestamp>/        (default)
    gdrive  -> Google Drive folder <GDRIVE_FOLDER>/<timestamp>/   (needs GDRIVE_TOKEN)
    both    -> both of the above
"""
import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import boto3
from botocore.config import Config

BUCKET = os.environ["R2_BUCKET"]
ENDPOINT = os.environ.get("R2_ENDPOINT") or \
    f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com"

s3 = boto3.client(
    "s3",
    endpoint_url=ENDPOINT,
    aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
    aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
    region_name="auto",
    config=Config(retries={"max_attempts": 5, "mode": "standard"}),
)

PREFIXES = ["Audio/", "Assets/Hooks/", "Assets/Cats/", "Assets/Eye/", "Assets/Sad/"]


def download_prefix(prefix):
    n = 0
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.endswith("/"):
                continue
            dest = Path(key)
            dest.parent.mkdir(parents=True, exist_ok=True)
            s3.download_file(BUCKET, key, str(dest))
            n += 1
    print(f"downloaded {n} file(s) from {prefix}", flush=True)
    return n


def upload_to_r2(stamp):
    for f in sorted(Path("Compo").glob("*.mp4")):
        key = f"output/{stamp}/{f.name}"
        s3.upload_file(str(f), BUCKET, key, ExtraArgs={"ContentType": "video/mp4"})
        print(f"uploaded to R2: {key}", flush=True)


def upload_to_gdrive(stamp):
    """Upload Compo/*.mp4 to Google Drive using rclone (configured via env vars)."""
    if not shutil.which("rclone"):
        sys.exit("rclone is not installed on this machine")
    token = os.environ.get("GDRIVE_TOKEN")
    if not token:
        sys.exit("GDRIVE_TOKEN secret is missing")
    folder = os.environ.get("GDRIVE_FOLDER", "CatVideos").strip("/")
    env = dict(os.environ)
    env.setdefault("RCLONE_CONFIG_GDRIVE_TYPE", "drive")
    env["RCLONE_CONFIG_GDRIVE_TOKEN"] = token
    dest = f"gdrive:{folder}/{stamp}"
    subprocess.run(["rclone", "copy", "Compo", dest,
                    "--include", "*.mp4", "--transfers", "2", "-v"],
                   check=True, env=env)
    print(f"uploaded to Google Drive: {folder}/{stamp}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=1)
    ap.add_argument("--workers", type=int, default=2)
    args = ap.parse_args()

    for p in PREFIXES:
        if download_prefix(p) == 0:
            sys.exit(f"No files found under '{p}' in bucket '{BUCKET}'")

    subprocess.run([sys.executable, "cat_video_maker.py",
                    "--count", str(args.count),
                    "--workers", str(args.workers),
                    "--out", "Compo"], check=True)

    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = os.environ.get("UPLOAD_TO", "r2").lower()
    if target not in ("r2", "gdrive", "both"):
        sys.exit("UPLOAD_TO must be r2, gdrive or both")

    if target in ("r2", "both"):
        upload_to_r2(stamp)
    if target in ("gdrive", "both"):
        upload_to_gdrive(stamp)

if __name__ == "__main__":
    main()
