#!/usr/bin/env python3
"""
Builder video pipeline: scrape -> download -> YouTube -> GCS -> delete local.
Local disk is only a temp staging area; nothing persists here.
"""
import os, re, json, subprocess, sys
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
TMP = os.path.join(BASE, "downloads")
META = os.path.join(BASE, "video_metadata.json")
GCS_BUCKET = "gs://paradise-realty-backups/builder-videos"
GCS_META = "gs://paradise-realty-images/data/test/builder_videos.json"
os.makedirs(TMP, exist_ok=True)

def load_meta():
    return json.load(open(META)) if os.path.exists(META) else []

def save_meta(videos):
    json.dump(videos, open(META, "w"), indent=2)
    subprocess.run(["gsutil", "-q", "cp", META, GCS_META], check=False)

def download(v):
    safe = re.sub(r"[^\w\-]", "_", f"{v['builder']}_{v['community']}_{v['video_id'][:6]}")
    out = os.path.join(TMP, f"{safe}.mp4")
    if not os.path.exists(out):
        subprocess.run(["yt-dlp", "-f", "bestvideo[ext=mp4][height<=1080]+bestaudio[ext=m4a]/mp4/best[height<=1080]",
                        "-o", out, "--no-playlist", "--quiet", v["youtube_url"]], check=True, timeout=600)
    return out

def to_gcs(path):
    dest = f"{GCS_BUCKET}/{os.path.basename(path)}"
    subprocess.run(["gsutil", "-q", "cp", path, dest], check=True)
    return dest

def to_youtube(videos):
    subprocess.run(["node", os.path.join(BASE, "upload_to_youtube.js")], check=False)
    return load_meta()

def main():
    videos = load_meta()
    pending = [v for v in videos if not v.get("gcs_path")]
    print(f"{len(videos)} videos in inventory, {len(pending)} pending")
    for v in pending:
        try:
            print(f"  {v['builder']} - {v['community']}")
            path = download(v)
            v["gcs_path"] = to_gcs(path)
            print(f"    -> GCS ok")
        except Exception as e:
            print(f"    x {e}")
    save_meta(videos)
    videos = to_youtube(videos)
    for f in os.listdir(TMP):
        if f.endswith(".mp4"):
            os.remove(os.path.join(TMP, f))
    save_meta(videos)
    print("Local staging cleared. Inventory synced to GCS.")

if __name__ == "__main__":
    main()
