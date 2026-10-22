// Interactive dashboard logic: file selection, dynamic params, job submission & polling.

const OPERATION_FIELDS = {
  trim: [
    { name: "start_time", label: "Start time", type: "text", placeholder: "00:00:05", default: "00:00:00" },
    { name: "end_time", label: "End time", type: "text", placeholder: "00:00:15", default: "00:00:10" },
  ],
  resize: [
    { name: "width", label: "Width (px)", type: "number", default: 1280, min: 1, max: 7680 },
    { name: "height", label: "Height (px)", type: "number", default: 720, min: 1, max: 4320 },
    { name: "maintain_aspect_ratio", label: "Maintain aspect ratio", type: "checkbox", default: true },
  ],
  extract_audio: [
    { name: "audio_format", label: "Audio format", type: "select", options: ["mp3", "aac", "wav", "flac", "ogg"], default: "mp3" },
  ],
  convert_format: [
    { name: "target_format", label: "Target format", type: "select", options: ["mp4", "mkv", "avi", "webm", "mov", "gif"], default: "mp4" },
  ],
  add_watermark: [
    { name: "text", label: "Watermark text", type: "text", placeholder: "© My Brand", default: "" },
    { name: "position", label: "Position", type: "select", options: ["top_left", "top_right", "bottom_left", "bottom_right", "center"], default: "bottom_right" },
    { name: "font_size", label: "Font size", type: "number", default: 24, min: 8, max: 200 },
    { name: "font_color", label: "Font color", type: "text", placeholder: "white", default: "white" },
  ],
  compress: [
    { name: "crf", label: "Quality (CRF, lower = better; ignored if target size is set)", type: "number", default: 28, min: 0, max: 51 },
    { name: "preset", label: "Encoding preset", type: "select", options: ["ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"], default: "medium" },
    { name: "target_size_mb", label: "Target output size (MB, optional)", type: "number", placeholder: "e.g. 25", default: "", min: 1, optional: true },
  ],
  concatenate: [
    { name: "output_format", label: "Output format", type: "select", options: ["mp4", "mkv", "webm", "mov"], default: "mp4" },
  ],
};

// Operations that accept (and require) more than one input file.
const MULTI_FILE_OPERATIONS = new Set(["concatenate"]);

const dropZone = document.getElementById("drop-zone");
const dropZoneText = document.getElementById("drop-zone-text");
const multiHint = document.getElementById("multi-hint");
const fileInput = document.getElementById("file-input");
const fileListEl = document.getElementById("file-list");
const operationSelect = document.getElementById("operation");
const paramsContainer = document.getElementById("params-container");
const submitBtn = document.getElementById("submit-btn");

const statusPanel = document.getElementById("status-panel");
const spinner = document.getElementById("spinner");
const statusText = document.getElementById("status-text");
const progressPct = document.getElementById("progress-pct");
const progressBar = document.getElementById("progress-bar");
const errorText = document.getElementById("error-text");
const downloadLink = document.getElementById("download-link");

let selectedFiles = [];
let pollHandle = null;

function isMultiFileOperation() {
  return MULTI_FILE_OPERATIONS.has(operationSelect.value);
}

function renderParamFields(operation) {
  paramsContainer.innerHTML = "";
  const fields = OPERATION_FIELDS[operation] || [];
  for (const field of fields) {
    const wrapper = document.createElement("div");

    if (field.type === "checkbox") {
      wrapper.className = "flex items-center gap-2";
      wrapper.innerHTML = `
        <input type="checkbox" id="param-${field.name}" ${field.default ? "checked" : ""}
          class="h-4 w-4 rounded border-slate-300 text-indigo-600 focus:ring-indigo-400" />
        <label for="param-${field.name}" class="text-sm font-medium text-slate-700">${field.label}</label>
      `;
    } else if (field.type === "select") {
      const options = field.options
        .map((opt) => `<option value="${opt}" ${opt === field.default ? "selected" : ""}>${opt}</option>`)
        .join("");
      wrapper.innerHTML = `
        <label class="block text-sm font-medium text-slate-700 mb-1">${field.label}</label>
        <select id="param-${field.name}" class="w-full border border-slate-300 rounded-lg px-3 py-2 focus:outline-none focus:ring-2 focus:ring-indigo-400">
          ${options}
        </select>
      `;
    } else {
      const extra = field.type === "number" ? `min="${field.min ?? ""}" max="${field.max ?? ""}"` : "";
      wrapper.innerHTML = `
        <label class="block text-sm font-medium text-slate-700 mb-1">${field.label}</label>
        <input type="${field.type}" id="param-${field.name}" value="${field.default ?? ""}"
          placeholder="${field.placeholder ?? ""}" ${extra}
          class="w-full border border-slate-300 rounded-lg px-3 py-2 focus:outline-none focus:ring-2 focus:ring-indigo-400" />
      `;
    }
    paramsContainer.appendChild(wrapper);
  }
}

function collectParams(operation) {
  const fields = OPERATION_FIELDS[operation] || [];
  const params = {};
  for (const field of fields) {
    const el = document.getElementById(`param-${field.name}`);
    if (!el) continue;
    if (field.type === "checkbox") {
      params[field.name] = el.checked;
    } else if (field.type === "number") {
      if (field.optional && el.value.trim() === "") continue; // omit optional empty numbers
      params[field.name] = Number(el.value);
    } else {
      params[field.name] = el.value;
    }
  }
  return params;
}

function renderFileList() {
  fileListEl.innerHTML = "";
  if (selectedFiles.length === 0) {
    fileListEl.classList.add("hidden");
    dropZoneText.classList.remove("hidden");
    return;
  }
  fileListEl.classList.remove("hidden");
  dropZoneText.classList.add("hidden");

  selectedFiles.forEach((file, index) => {
    const li = document.createElement("li");
    li.className = "flex items-center justify-between gap-3 px-4 py-2 bg-white text-sm";
    li.innerHTML = `
      <span class="flex items-center gap-2 min-w-0">
        <span class="shrink-0 inline-flex items-center justify-center h-5 w-5 rounded-full bg-indigo-100 text-indigo-700 text-xs font-semibold">${index + 1}</span>
        <span class="truncate text-slate-700">${file.name}</span>
        <span class="shrink-0 text-slate-400">(${(file.size / (1024 * 1024)).toFixed(2)} MB)</span>
      </span>
      <button type="button" data-index="${index}" class="remove-file-btn text-slate-400 hover:text-red-600 shrink-0" title="Remove">✕</button>
    `;
    fileListEl.appendChild(li);
  });

  fileListEl.querySelectorAll(".remove-file-btn").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const idx = Number(btn.dataset.index);
      selectedFiles.splice(idx, 1);
      renderFileList();
      updateSubmitState();
    });
  });
}

function updateSubmitState() {
  const needsMultiple = isMultiFileOperation();
  submitBtn.disabled = needsMultiple ? selectedFiles.length < 2 : selectedFiles.length !== 1;
}

function addFiles(fileArray) {
  if (isMultiFileOperation()) {
    selectedFiles = [...selectedFiles, ...fileArray];
  } else {
    selectedFiles = fileArray.slice(0, 1);
  }
  renderFileList();
  updateSubmitState();
}

function applyOperationFileMode() {
  const multi = isMultiFileOperation();
  fileInput.multiple = multi;
  multiHint.classList.toggle("hidden", !multi);
  if (!multi && selectedFiles.length > 1) {
    selectedFiles = selectedFiles.slice(0, 1);
    renderFileList();
  }
  updateSubmitState();
}

dropZone.addEventListener("click", () => fileInput.click());
fileInput.addEventListener("change", () => {
  if (fileInput.files.length > 0) addFiles(Array.from(fileInput.files));
  fileInput.value = ""; // allow re-selecting the same file(s) later
});

["dragenter", "dragover"].forEach((evt) =>
  dropZone.addEventListener(evt, (e) => {
    e.preventDefault();
    dropZone.classList.add("drag-over");
  })
);
["dragleave", "drop"].forEach((evt) =>
  dropZone.addEventListener(evt, (e) => {
    e.preventDefault();
    dropZone.classList.remove("drag-over");
  })
);
dropZone.addEventListener("drop", (e) => {
  if (e.dataTransfer.files.length > 0) addFiles(Array.from(e.dataTransfer.files));
});

operationSelect.addEventListener("change", () => {
  renderParamFields(operationSelect.value);
  applyOperationFileMode();
});
renderParamFields(operationSelect.value);
applyOperationFileMode();

function resetStatusPanel() {
  statusPanel.classList.remove("hidden");
  spinner.classList.remove("hidden");
  errorText.classList.add("hidden");
  downloadLink.classList.add("hidden");
  progressBar.style.width = "0%";
  progressPct.textContent = "0%";
  statusText.textContent = "Pending";
  statusText.className = "font-medium text-slate-700";
}

function setStatusUI(job) {
  const pct = Math.round(job.progress ?? 0);
  progressBar.style.width = `${pct}%`;
  progressPct.textContent = `${pct}%`;

  const labels = { pending: "Pending", processing: "Processing", completed: "Completed", failed: "Failed" };
  statusText.textContent = labels[job.status] ?? job.status;

  if (job.status === "completed") {
    spinner.classList.add("hidden");
    statusText.className = "font-medium text-emerald-600";
    progressBar.style.width = "100%";
    progressPct.textContent = "100%";
    if (job.download_url) {
      downloadLink.href = job.download_url;
      downloadLink.classList.remove("hidden");
    }
  } else if (job.status === "failed") {
    spinner.classList.add("hidden");
    statusText.className = "font-medium text-red-600";
    if (job.error_message) {
      errorText.textContent = job.error_message;
      errorText.classList.remove("hidden");
    }
  } else {
    statusText.className = "font-medium text-indigo-600";
  }
}

async function pollJob(jobId) {
  try {
    const res = await fetch(`/jobs/${jobId}`);
    if (!res.ok) throw new Error(`Failed to fetch job status (${res.status})`);
    const job = await res.json();
    setStatusUI(job);

    if (job.status === "completed" || job.status === "failed") {
      clearInterval(pollHandle);
      submitBtn.disabled = false;
      submitBtn.textContent = "Start Processing";
    }
  } catch (err) {
    clearInterval(pollHandle);
    spinner.classList.add("hidden");
    statusText.textContent = "Error";
    statusText.className = "font-medium text-red-600";
    errorText.textContent = err.message;
    errorText.classList.remove("hidden");
    submitBtn.disabled = false;
    submitBtn.textContent = "Start Processing";
  }
}

submitBtn.addEventListener("click", async () => {
  const needsMultiple = isMultiFileOperation();
  if (needsMultiple ? selectedFiles.length < 2 : selectedFiles.length !== 1) return;

  const operation = operationSelect.value;
  const params = collectParams(operation);

  const formData = new FormData();
  for (const file of selectedFiles) formData.append("files", file);
  formData.append("operation", operation);
  formData.append("params", JSON.stringify(params));

  submitBtn.disabled = true;
  submitBtn.textContent = "Uploading...";
  resetStatusPanel();

  try {
    const res = await fetch("/process-video", { method: "POST", body: formData });
    if (!res.ok) {
      const errBody = await res.json().catch(() => ({}));
      throw new Error(errBody.detail ? JSON.stringify(errBody.detail) : `Request failed (${res.status})`);
    }
    const { job_id } = await res.json();
    submitBtn.textContent = "Processing...";
    pollHandle = setInterval(() => pollJob(job_id), 1000);
    pollJob(job_id);
  } catch (err) {
    spinner.classList.add("hidden");
    statusText.textContent = "Error";
    statusText.className = "font-medium text-red-600";
    errorText.textContent = err.message;
    errorText.classList.remove("hidden");
    submitBtn.disabled = false;
    submitBtn.textContent = "Start Processing";
  }
});
