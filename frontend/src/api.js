// Thin wrapper over fetch for the console API. The session is an HTTP-only cookie, so the
// browser sends it automatically and JavaScript never sees it.
export class ApiError extends Error {
  constructor(status, detail) {
    super(typeof detail === "string" ? detail : JSON.stringify(detail));
    this.status = status;
    this.detail = detail;
  }
}

async function request(method, path, body) {
  const res = await fetch(path, {
    method,
    credentials: "same-origin",
    headers: body !== undefined ? { "Content-Type": "application/json" } : {},
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (res.status === 204) return null;
  const text = await res.text();
  const data = text ? JSON.parse(text) : null;
  if (!res.ok) {
    let detail = data?.detail ?? res.statusText;
    if (Array.isArray(detail)) detail = detail.map((d) => d.msg).join("; ");
    throw new ApiError(res.status, detail);
  }
  return data;
}

export const api = {
  get: (path) => request("GET", path),
  post: (path, body = {}) => request("POST", path, body),
  put: (path, body = {}) => request("PUT", path, body),
  patch: (path, body = {}) => request("PATCH", path, body),
  del: (path) => request("DELETE", path),
};

// ---------------------------------------------------------------- the services, with an API key
// The Files and Image tool pages are API clients: they call the public /v1 gateway with a key the
// user pastes in, so their usage is authenticated, rate-limited and metered like any other client.
async function gwError(res) {
  let detail = res.statusText;
  try {
    detail = (await res.json()).detail ?? detail;
  } catch {
    /* not JSON */
  }
  return new ApiError(res.status, detail);
}

const auth = (key) => ({ Authorization: `Bearer ${key}` });
const objectPath = (name) => `/v1/storage/objects/${encodeURIComponent(name)}`;

export const gw = {
  async list(key) {
    const res = await fetch("/v1/storage/objects", { headers: auth(key) });
    if (!res.ok) throw await gwError(res);
    return (await res.json()).objects;
  },

  async download(key, name) {
    const res = await fetch(objectPath(name), { headers: auth(key) });
    if (!res.ok) throw await gwError(res);
    return res.blob();
  },

  async remove(key, name) {
    const res = await fetch(objectPath(name), { method: "DELETE", headers: auth(key) });
    if (!res.ok) throw await gwError(res);
  },

  // XMLHttpRequest rather than fetch, for upload progress.
  upload(key, name, blob, onProgress) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("PUT", objectPath(name));
      xhr.setRequestHeader("Authorization", `Bearer ${key}`);
      xhr.setRequestHeader("Content-Type", blob.type || "application/octet-stream");
      xhr.upload.onprogress = (e) => e.lengthComputable && onProgress?.(e.loaded / e.total);
      xhr.onload = () => {
        let data = null;
        try {
          data = JSON.parse(xhr.responseText);
        } catch {
          /* empty */
        }
        if (xhr.status >= 200 && xhr.status < 300) resolve(data);
        else reject(new ApiError(xhr.status, data?.detail ?? xhr.statusText));
      };
      xhr.onerror = () => reject(new ApiError(0, "network error"));
      xhr.send(blob);
    });
  },

  async transform(key, blob, { width, height, format } = {}) {
    const params = new URLSearchParams();
    if (width) params.set("width", width);
    if (height) params.set("height", height);
    if (format) params.set("format", format);
    const res = await fetch(`/v1/images/transform?${params}`, {
      method: "POST",
      headers: { ...auth(key), "Content-Type": "application/octet-stream" },
      body: blob,
    });
    if (!res.ok) throw await gwError(res);
    return { blob: await res.blob(), cpuMs: Number(res.headers.get("x-cpu-ms") || 0) };
  },
};

// Object names allow letters, digits, '.', '_' and '-'; "My Report (v2).pdf" → "my-report-v2.pdf".
export function objectName(fileName) {
  return fileName
    .toLowerCase()
    .replace(/[^a-z0-9._-]+/g, "-")
    .replace(/-+/g, "-")
    .replace(/^[^a-z0-9]+/, "")
    .replace(/-+\./g, ".")
    .slice(0, 200);
}

export function saveBlob(blob, name) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
}
