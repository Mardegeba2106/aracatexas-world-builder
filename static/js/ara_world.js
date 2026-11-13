const fileInput = document.querySelector("#map-file");
const dropzone = document.querySelector("#dropzone");
const fileName = document.querySelector("#file-name");
const generateButton = document.querySelector("#generate-button");
const statusNode = document.querySelector("#status");
const previewImage = document.querySelector("#preview-image");
const previewFrame = document.querySelector("#preview-frame");
const outputs = document.querySelector("#outputs");
let selectedFile = null;

function setStatus(message, kind = "") {
  statusNode.textContent = message;
  statusNode.className = kind;
}

function selectFile(file) {
  if (!file) return;
  if (!file.name.toLowerCase().endsWith(".json")) {
    selectedFile = null;
    generateButton.disabled = true;
    fileName.textContent = "Arquivo inválido. Selecione um .json";
    setStatus("Selecione o JSON do mapa do Araçatexas Game.", "error");
    return;
  }
  selectedFile = file;
  fileName.textContent = file.name;
  generateButton.disabled = false;
  setStatus(`${file.name} carregado (${(file.size / 1024).toFixed(0)} KB).`, "success");
}

fileInput.addEventListener("change", () => selectFile(fileInput.files?.[0]));
for (const eventName of ["dragenter", "dragover"]) {
  dropzone.addEventListener(eventName, event => {
    event.preventDefault();
    dropzone.classList.add("dragging");
  });
}
for (const eventName of ["dragleave", "drop"]) {
  dropzone.addEventListener(eventName, event => {
    event.preventDefault();
    dropzone.classList.remove("dragging");
  });
}
dropzone.addEventListener("drop", event => selectFile(event.dataTransfer?.files?.[0]));

generateButton.addEventListener("click", async () => {
  if (!selectedFile) return;
  const body = new FormData();
  body.append("map", selectedFile);
  generateButton.disabled = true;
  generateButton.textContent = "GERANDO...";
  setStatus("Lendo geometrias, construindo texturas e montando os atlas. Isso pode levar alguns segundos.");
  outputs.hidden = true;
  previewImage.hidden = true;
  try {
    const response = await fetch(document.body.dataset.apiUrl, { method: "POST", body });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || `Falha HTTP ${response.status}`);

    previewImage.src = `${result.preview}?v=${Date.now()}`;
    previewImage.hidden = false;
    previewFrame.classList.add("has-preview");
    document.querySelector("#count-roads").textContent = result.counts.roads;
    document.querySelector("#count-blocks").textContent = result.counts.blocks;
    document.querySelector("#count-lots").textContent = result.counts.lots;
    document.querySelector("#count-buildings").textContent = result.counts.buildings;
    document.querySelector("#count-residences").textContent = result.counts.generated_residences;
    document.querySelector("#count-properties").textContent = result.counts.residential_properties;
    document.querySelector("#count-trees").textContent = result.counts.generated_trees;
    document.querySelector("#stats").hidden = false;

    for (const [id, url] of [["zip-link", result.zip], ["map-link", result.gameMap],
      ["tiles-link", result.tileset], ["buildings-link", result.buildings]]) {
      document.getElementById(id).href = url;
    }
    outputs.hidden = false;
    setStatus("Mundo visual gerado. Confira a prévia e baixe o pacote.", "success");
    outputs.scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) {
    setStatus(error.message || "Não foi possível gerar o mundo.", "error");
  } finally {
    generateButton.disabled = !selectedFile;
    generateButton.innerHTML = 'GERAR MUNDO VISUAL <span>→</span>';
  }
});
