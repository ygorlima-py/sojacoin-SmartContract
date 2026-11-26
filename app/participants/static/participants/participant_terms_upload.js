"use strict";

document.addEventListener("DOMContentLoaded", () => {
    const dropZone = document.getElementById("terms-dropzone");
    const fileInput = document.getElementById("id_pdf_file");
    const fileName = document.getElementById("terms-file-name");
    if (!dropZone || !(fileInput instanceof HTMLInputElement) || !fileName) return;
    const fileDescription = document.getElementById("terms-file-description");
    const maxSizeMb = dropZone.dataset.maxSizeMb;
    if (fileDescription && maxSizeMb) {
        fileDescription.textContent = interpolate(
            gettext("Somente PDF, até %(size)s MB."),
            { size: maxSizeMb },
            true,
        );
    }

    const showSelectedFile = () => {
        const selectedFile = fileInput.files?.[0];
        fileName.textContent = selectedFile
            ? `${selectedFile.name} (${(selectedFile.size / (1024 * 1024)).toFixed(2)} MB)`
            : gettext("Nenhum arquivo selecionado");
    };

    fileInput.addEventListener("change", showSelectedFile);
    ["dragenter", "dragover"].forEach((eventName) => {
        dropZone.addEventListener(eventName, (event) => {
            event.preventDefault();
            dropZone.classList.add("border-[#1E6B3C]", "bg-[#F4F8F4]");
        });
    });
    ["dragleave", "drop"].forEach((eventName) => {
        dropZone.addEventListener(eventName, (event) => {
            event.preventDefault();
            dropZone.classList.remove("border-[#1E6B3C]", "bg-[#F4F8F4]");
        });
    });
    dropZone.addEventListener("drop", (event) => {
        const transfer = event.dataTransfer;
        if (!transfer?.files?.length) return;
        fileInput.files = transfer.files;
        fileInput.dispatchEvent(new Event("change", { bubbles: true }));
    });
    dropZone.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            fileInput.click();
        }
    });
    fileInput.addEventListener("focus", () => dropZone.classList.add("ring-2", "ring-[#1E6B3C]/30"));
    fileInput.addEventListener("blur", () => dropZone.classList.remove("ring-2", "ring-[#1E6B3C]/30"));
});
