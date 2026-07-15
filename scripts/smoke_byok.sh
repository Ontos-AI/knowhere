#!/usr/bin/env bash
# Local BYOK smoke checks against a running KNOWHERE API on :5005.
# Usage:
#   export KNOWHERE_API_KEY=kh_...
#   ./scripts/smoke_byok.sh
set -euo pipefail

BASE_URL="${KNOWHERE_BASE_URL:-http://localhost:5005}"
API_KEY="${KNOWHERE_API_KEY:?Set KNOWHERE_API_KEY to a local API key}"

auth_hdr=(-H "Authorization: Bearer ${API_KEY}" -H "Content-Type: application/json")

echo "== health =="
curl -fsS "${BASE_URL}/health" | head -c 200; echo

echo "== OpenAPI: v2 jobs has llm_config, v1 does not =="
python3 - <<'PY'
import json, os, urllib.request
base = os.environ.get("KNOWHERE_BASE_URL", "http://localhost:5005")
with urllib.request.urlopen(f"{base}/openapi.json") as resp:
    schema = json.load(resp)
paths = schema["paths"]
v1 = paths.get("/api/v1/jobs", paths.get("/v1/jobs", {}))
v2 = paths.get("/api/v2/jobs", paths.get("/v2/jobs", {}))
# FastAPI root_path may strip /api from openapi paths — try both
def body_props(op):
    if not op:
        return set()
    post = op.get("post") or {}
    body = ((post.get("requestBody") or {}).get("content") or {}).get("application/json") or {}
    s = body.get("schema") or {}
    if "$ref" in s:
        name = s["$ref"].rsplit("/", 1)[-1]
        s = schema["components"]["schemas"].get(name, {})
    return set((s.get("properties") or {}).keys())

# Prefer component schemas when available
comps = schema.get("components", {}).get("schemas", {})
v1_keys = set((comps.get("JobCreate") or {}).get("properties", {}).keys())
v2_keys = set((comps.get("JobCreateV2") or {}).get("properties", {}).keys())
r1 = set((comps.get("RetrievalQueryRequest") or {}).get("properties", {}).keys())
r2 = set((comps.get("RetrievalQueryRequestV2") or {}).get("properties", {}).keys())
print("JobCreate llm_config:", "llm_config" in v1_keys)
print("JobCreateV2 llm_config:", "llm_config" in v2_keys)
print("RetrievalQueryRequest llm_config:", "llm_config" in r1)
print("RetrievalQueryRequestV2 llm_config:", "llm_config" in r2)
assert "llm_config" not in v1_keys
assert "llm_config" in v2_keys
assert "llm_config" not in r1
assert "llm_config" in r2
print("OpenAPI BYOK surface OK")
PY

echo "== v2 jobs: reject empty llm_config object =="
code=$(curl -sS -o /tmp/byok_empty.json -w '%{http_code}' "${auth_hdr[@]}" \
  -d '{"source_type":"url","source_url":"https://example.com/a.pdf","llm_config":{}}' \
  "${BASE_URL}/api/v2/jobs")
echo "HTTP $code"
cat /tmp/byok_empty.json | head -c 400; echo
test "$code" = "422" || test "$code" = "400"

echo "== v2 jobs: accept llm_config.provider (multimodal shorthand) shape =="
# Expect waiting-file or pending-ish success, not 422
code=$(curl -sS -o /tmp/byok_ok.json -w '%{http_code}' "${auth_hdr[@]}" \
  -d '{
    "source_type":"url",
    "source_url":"https://www.w3.org/WAI/ER/tests/xhtml/testfiles/resources/pdf/dummy.pdf",
    "file_name":"dummy.pdf",
    "data_id":"byok-smoke-provider",
    "llm_config":{
      "provider":{
        "api_key":"sk-smoke-test-key-not-real",
        "model":"gpt-4o",
        "base_url":"https://api.openai.com/v1"
      }
    }
  }' \
  "${BASE_URL}/api/v2/jobs")
echo "HTTP $code"
cat /tmp/byok_ok.json | head -c 600; echo
test "$code" = "200" || test "$code" = "201"

JOB_ID=$(python3 -c 'import json; print(json.load(open("/tmp/byok_ok.json")).get("job_id",""))')
echo "job_id=${JOB_ID}"

if [[ -n "${JOB_ID}" ]]; then
  echo "== GET job: ensure raw api_key is not echoed =="
  curl -fsS "${auth_hdr[@]}" "${BASE_URL}/api/v2/jobs/${JOB_ID}" | tee /tmp/byok_job.json | head -c 800; echo
  if grep -q 'sk-smoke-test-key-not-real' /tmp/byok_job.json; then
    echo "FAIL: raw API key leaked in job response" >&2
    exit 1
  fi
  echo "No raw key in job response OK"
fi

echo "== v2 jobs: accept llm_config.text-only (partial override) shape =="
code=$(curl -sS -o /tmp/byok_text.json -w '%{http_code}' "${auth_hdr[@]}" \
  -d '{
    "source_type":"url",
    "source_url":"https://www.w3.org/WAI/ER/tests/xhtml/testfiles/resources/pdf/dummy.pdf",
    "file_name":"dummy.pdf",
    "data_id":"byok-smoke-text-only",
    "llm_config":{
      "text":{
        "api_key":"sk-smoke-test-key-not-real",
        "model":"gpt-4o-mini",
        "base_url":"https://api.openai.com/v1"
      }
    }
  }' \
  "${BASE_URL}/api/v2/jobs")
echo "HTTP $code"
cat /tmp/byok_text.json | head -c 400; echo
test "$code" = "200" || test "$code" = "201"

echo "== v1 jobs: llm_config should be rejected as unsupported extra =="
code=$(curl -sS -o /tmp/byok_v1.json -w '%{http_code}' "${auth_hdr[@]}" \
  -d '{
    "source_type":"url",
    "source_url":"https://example.com/a.pdf",
    "llm_config":{"text":{"api_key":"sk-x","model":"m","base_url":"https://example.com/v1"}}
  }' \
  "${BASE_URL}/api/v1/jobs")
echo "HTTP $code"
cat /tmp/byok_v1.json | head -c 400; echo
# Prefer 422 / validation error; 200 would mean v1 accepted BYOK (bad)
test "$code" != "200" && test "$code" != "201"

echo
echo "BYOK smoke checks finished."
