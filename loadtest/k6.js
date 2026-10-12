// Load test (spec section 15): run from the laptop, never on the VM, so k6 does not compete
// with the platform for the single core.
//   cd backend && .venv/bin/python ../scripts/demo.py seed     # writes scripts/keys.json
//   k6 run -e BASE_URL=http://127.0.0.1:8000 loadtest/k6.js
// While it runs, watch: docker stats   (memory must stay inside the section 14 limits)
import http from "k6/http";
import { check } from "k6";

const BASE = __ENV.BASE_URL || "http://127.0.0.1:8000";
const KEYS = JSON.parse(open(__ENV.KEYS_FILE || "../scripts/keys.json"));
const RATE = parseInt(__ENV.RATE || "30", 10);
const PAYLOADS = [1024, 16 * 1024, 128 * 1024].map((n) => "x".repeat(n));

export const options = {
  scenarios: {
    steady: {
      executor: "constant-arrival-rate",
      rate: RATE, // tens of requests per second
      timeUnit: "1s",
      duration: __ENV.DURATION || "5m",
      preAllocatedVUs: 20,
      maxVUs: 60,
    },
  },
  thresholds: {
    http_req_failed: ["rate<0.05"],
    http_req_duration: ["p(95)<1500"],
  },
};

export default function () {
  const key = KEYS[Math.floor(Math.random() * KEYS.length)];
  const params = { headers: { Authorization: `Bearer ${key}` } };
  const name = `k6-${Math.floor(Math.random() * 50)}`;
  const roll = Math.random();
  let res;
  if (roll < 0.4) {
    const body = PAYLOADS[Math.floor(Math.random() * PAYLOADS.length)];
    res = http.put(`${BASE}/v1/storage/objects/${name}`, body, params);
    check(res, { "upload ok or limited": (r) => [201, 429].includes(r.status) });
  } else if (roll < 0.75) {
    res = http.get(`${BASE}/v1/storage/objects/${name}`, params);
    check(res, { "download ok or missing": (r) => [200, 404, 429].includes(r.status) });
  } else {
    res = http.get(`${BASE}/v1/storage/objects`, params);
    check(res, { "list ok": (r) => [200, 429].includes(r.status) });
  }
}
