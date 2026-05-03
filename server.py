import os, re, uuid, threading, json
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import redis

app = Flask(__name__)
CORS(app)

# =========================
# REDIS SETUP
# =========================
REDIS_URL = os.environ.get("redis://default:ZkCnWoSMJLSeAhXscyRzQznjcFHYinIo@tramway.proxy.rlwy.net:11743")or "redis://localhost:6379"
r = redis.from_url(REDIS_URL, decode_responses=True)

TMP = "tmp_jobs"
os.makedirs(TMP, exist_ok=True)

# =========================
# REDIS HELPERS
# =========================
def save_job(job_id, data):
    r.set(job_id, json.dumps(data))

def get_job(job_id):
    data = r.get(job_id)
    return json.loads(data) if data else None

def update_job(job_id, **kwargs):
    job = get_job(job_id)
    if not job:
        return
    job.update(kwargs)
    save_job(job_id, job)

# =========================
# DEBUG
# =========================
@app.route("/debug")
def debug():
    keys = r.keys("*")
    return {"jobs": keys}

# =========================
# JOB RUNNER (SIMPLIFIED)
# =========================
def _run_job(job_id, url):
    try:
        update_job(job_id, status="running", message="Processing started")

        # simulate work (replace with your real logic)
        import time
        time.sleep(5)

        update_job(
            job_id,
            status="done",
            message="Completed",
            video_url=f"/video/{job_id}/summary.mp4"
        )

    except Exception as e:
        update_job(job_id, status="error", message=str(e))

# =========================
# ROUTES
# =========================
@app.route("/")
def index():
    return "Backend running"

@app.route("/process", methods=["POST"])
def process():
    data = request.get_json()
    url = data.get("video_url", "")

    job_id = str(uuid.uuid4())

    job_data = {
        "status": "queued",
        "message": "Job created",
        "video_url": None
    }

    save_job(job_id, job_data)

    threading.Thread(target=_run_job, args=(job_id, url), daemon=True).start()

    return jsonify(success=True, job_id=job_id)

@app.route("/status/<job_id>")
def status(job_id):
    job = get_job(job_id)
    if not job:
        return jsonify(status="error", message="Job not found"), 404
    return jsonify(job)

@app.route("/video/<job_id>/<filename>")
def serve_video(job_id, filename):
    path = os.path.join(TMP, job_id, filename)
    if not os.path.exists(path):
        return jsonify(error="File not found"), 404
    return send_file(path)

# =========================
# RUN
# =========================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 4000))
    app.run(host="0.0.0.0", port=port)