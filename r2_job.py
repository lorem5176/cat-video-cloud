#!/usr/bin/env python3
"""
Cloud job: fetch source files -> render videos -> upload results.

SOURCE_FROM (where clips come from):
    r2      -> Cloudflare R2 bucket (default)
    gdrive  -> Google Drive folder SOURCE_FOLDER (default "CatVideoSources")

Folder layout (same in R2 or Drive):
    Audio/                 audio tracks
    Assets/Hooks/          hook clips
    Assets/Cats/           cats clips
    Assets/Eye/            eye clips
    Assets/Sad/            sad clips

UPLOAD_TO (where finished videos go):
    r2      -> R2 bucket under output/<timestamp>/          (default)
    gdrive  -> Drive folder GDRIVE_FOLDER/<timestamp>/      (default "CatVideos")
    both    -> both of the above

Environment variables:
    GDRIVE_TOKEN                                  (needed if Drive is used for anything)
    R2_ACCOUNT_ID, R2_ACCESS_KEY_ID,
    R2_SECRET_ACCESS_KEY, R2_BUCKET               (needed if R2 is used for anything)
"""
import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

PREFIXES = ["Audio", "Assets/Hooks", "Assets/Cats", "Assets/Eye", "Assets/Sad"]

_s3 = None


# ---------------------------------------------------------------- R2 ----
def r2():
    """Create the R2 client only when R2 is actually used."""
    global _s3
    if _s3 is None:
        import boto3
        from botocore.config import Config
        endpoint = os.environ.get("R2_ENDPOINT") or \
            f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com"
        _s3 = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
            aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
            region_name="auto",
            config=Config(retries={"max_attempts": 5, "mode": "standard"}),
        )
    return _s3


def r2_download_prefix(prefix):
    bucket = os.environ["R2_BUCKET"]
    n = 0
    for page in r2().get_paginator("list_objects_v2").paginate(
            Bucket=bucket, Prefix=prefix + "/"):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.endswith("/"):
                continue
            dest = Path(key)
            dest.parent.mkdir(parents=True, exist_ok=True)
            r2().download_file(bucket, key, str(dest))
            n += 1
    print(f"downloaded {n} file(s) from R2:{prefix}/", flush=True)
    return n


def upload_to_r2(stamp):
    bucket = os.environ["R2_BUCKET"]
    for f in sorted(Path("Compo").glob("*.mp4")):
        key = f"output/{stamp}/{f.name}"
        r2().upload_file(str(f), bucket, key, ExtraArgs={"ContentType": "video/mp4"})
        print(f"uploaded to R2: {key}", flush=True)


# ------------------------------------------------------- Google Drive ----
def rclone_env():
    if not shutil.which("rclone"):
        sys.exit("rclone is not installed on this machine")
    token = os.environ.get("GDRIVE_TOKEN")
    if not token:
        sys.exit("GDRIVE_TOKEN secret is missing")
    env = dict(os.environ)
    env.setdefault("RCLONE_CONFIG_GDRIVE_TYPE", "drive")
    env["RCLONE_CONFIG_GDRIVE_TOKEN"] = token
    return env


def gdrive_download_prefix(prefix):
    root = os.environ.get("SOURCE_FOLDER", "CatVideoSources").strip("/")
    Path(prefix).mkdir(parents=True, exist_ok=True)
    subprocess.run(["rclone", "copy", f"gdrive:{root}/{prefix}", prefix,
                    "--drive-skip-gdocs", "--transfers", "4", "-v"],
                   check=True, env=rclone_env())
    n = sum(1 for p in Path(prefix).iterdir() if p.is_file())
    print(f"downloaded {n} file(s) from Drive:{root}/{prefix}", flush=True)
    return n


def upload_to_gdrive(stamp):
    folder = os.environ.get("GDRIVE_FOLDER", "CatVideos").strip("/")
    subprocess.run(["rclone", "copy", "Compo", f"gdrive:{folder}/{stamp}",
                    "--include", "*.mp4", "--transfers", "2", "-v"],
                   check=True, env=rclone_env())
    print(f"uploaded to Google Drive: {folder}/{stamp}", flush=True)


# --------------------------------------------------------------- main ----
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=1)
    ap.add_argument("--workers", type=int, default=2)
    args = ap.parse_args()

    source = os.environ.get("SOURCE_FROM", "r2").lower()
    target = os.environ.get("UPLOAD_TO", "r2").lower()
    if source not in ("r2", "gdrive"):
        sys.exit("SOURCE_FROM must be r2 or gdrive")
    if target not in ("r2", "gdrive", "both"):
        sys.exit("UPLOAD_TO must be r2, gdrive or both")

    fetch = r2_download_prefix if source == "r2" else gdrive_download_prefix
    for p in PREFIXES:
        if fetch(p) == 0:
            sys.exit(f"No files found for '{p}' in {source}")

    subprocess.run([sys.executable, "cat_video_maker.py",
                    "--count", str(args.count),
                    "--workers", str(args.workers),
                    "--out", "Compo"], check=True)

    stamp = time.strftime("%Y%m%d-%H%M%S")
    if target in ("r2", "both"):
        upload_to_r2(stamp)
    if target in ("gdrive", "both"):
        upload_to_gdrive(stamp)


if __name__ == "__main__":
    main()
