import os
import uuid
import subprocess
import tempfile
import requests
from flask import Flask, request, jsonify, send_from_directory

app = Flask(__name__)

OUTPUT_DIR = "/tmp/stitched"
os.makedirs(OUTPUT_DIR, exist_ok=True)

# Simple shared-secret auth so random people can't hit your stitcher and burn your compute
API_KEY = os.environ.get("STITCH_API_KEY", "change-me")


def check_auth(req):
    return req.headers.get("Authorization") == f"Bearer {API_KEY}"


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/stitch", methods=["POST"])
def stitch():
    """
    Body: { "clip_urls": ["https://...clip1.mp4", "https://...clip2.mp4", ...] }
    Returns: { "video_url": "https://<render-host>/files/<name>.mp4" }
    """
    if not check_auth(request):
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(force=True)
    clip_urls = data.get("clip_urls", [])
    # Optional: Gemini/Veo video URIs require an API key to download.
    # Pass it here and it gets appended as ?key=... on each download.
    gemini_api_key = data.get("gemini_api_key")
    if not clip_urls or len(clip_urls) < 1:
        return jsonify({"error": "clip_urls required"}), 400

    job_id = str(uuid.uuid4())[:8]

    with tempfile.TemporaryDirectory() as tmpdir:
        local_paths = []
        for i, url in enumerate(clip_urls):
            local_path = os.path.join(tmpdir, f"clip_{i}.mp4")
            download_url = url
            if gemini_api_key:
                sep = "&" if "?" in url else "?"
                download_url = f"{url}{sep}key={gemini_api_key}"
            r = requests.get(download_url, stream=True, timeout=120)
            r.raise_for_status()
            with open(local_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
            local_paths.append(local_path)

        # Re-encode each clip to a consistent format before concat (Grok clips can
        # vary slightly in codec params, which breaks naive concat otherwise)
        normalized_paths = []
        for i, path in enumerate(local_paths):
            norm_path = os.path.join(tmpdir, f"norm_{i}.mp4")
            subprocess.run([
                "ffmpeg", "-y", "-i", path,
                "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                "-c:a", "aac", "-ar", "44100", "-ac", "2",
                "-vf", "scale=1280:720,setsar=1",
                "-r", "24",
                norm_path
            ], check=True, capture_output=True)
            normalized_paths.append(norm_path)

        # Build concat list file
        concat_list_path = os.path.join(tmpdir, "concat_list.txt")
        with open(concat_list_path, "w") as f:
            for p in normalized_paths:
                f.write(f"file '{p}'\n")

        final_filename = f"{job_id}.mp4"
        final_path = os.path.join(OUTPUT_DIR, final_filename)

        subprocess.run([
            "ffmpeg", "-y", "-f", "concat", "-safe", "0",
            "-i", concat_list_path,
            "-c", "copy",
            final_path
        ], check=True, capture_output=True)

    base_url = request.host_url.rstrip("/")
    return jsonify({
        "video_url": f"{base_url}/files/{final_filename}",
        "job_id": job_id
    })


@app.route("/files/<filename>", methods=["GET"])
def serve_file(filename):
    return send_from_directory(OUTPUT_DIR, filename)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
