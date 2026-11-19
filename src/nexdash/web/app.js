// Minimal nexdash web UI. Talks only to the same origin through the REST API.
// The token stays in sessionStorage for this tab and is sent as a Bearer header.
// Data from the server is written with textContent, never as HTML.
"use strict";

(function () {
  const TOKEN_KEY = "nexdash.token";
  const byId = (id) => document.getElementById(id);
  let cubes = [];

  function setStatus(text, isError) {
    const el = byId("status");
    el.textContent = text || "";
    el.className = isError ? "error" : "";
  }

  async function api(path, body) {
    const headers = { Accept: "application/json" };
    const token = sessionStorage.getItem(TOKEN_KEY);
    if (token) headers.Authorization = "Bearer " + token;
    const init = { method: body === undefined ? "GET" : "POST", headers: headers, credentials: "omit" };
    if (body !== undefined) {
      headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(body);
    }
    const response = await fetch(path, init);
    let data = {};
    try {
      data = await response.json();
    } catch (err) {
      data = {};
    }
    if (!response.ok) {
      throw new Error(data.error || "Request failed with status " + response.status);
    }
    return data;
  }

  function checkboxList(container, names, checkedFirst) {
    container.replaceChildren();
    names.forEach((item, index) => {
      const label = document.createElement("label");
      const box = document.createElement("input");
      box.type = "checkbox";
      box.value = item.name;
      box.checked = checkedFirst && index === 0;
      label.append(box, " " + item.name);
      if (item.description) label.title = item.description;
      container.append(label);
    });
  }

  async function showCube(name) {
    const cube = await api("/api/cubes/" + encodeURIComponent(name));
    byId("cube-description").textContent = cube.description || "";
    checkboxList(byId("dimensions"), cube.dimensions, true);
    checkboxList(byId("metrics"), cube.metrics, true);
  }

  async function loadCubes() {
    const data = await api("/api/cubes");
    cubes = data.cubes || [];
    const select = byId("cube");
    select.replaceChildren();
    cubes.forEach((cube) => {
      const option = document.createElement("option");
      option.value = cube.name;
      option.textContent = cube.name;
      select.append(option);
    });
    if (cubes.length) await showCube(cubes[0].name);
    setStatus(cubes.length + " cube(s) available.");
  }

  function checked(containerId) {
    return Array.from(byId(containerId).querySelectorAll("input:checked")).map((box) => box.value);
  }

  function renderResult(data) {
    const table = byId("result");
    table.replaceChildren();
    const columns = data.columns || [];
    const head = document.createElement("thead");
    const headRow = document.createElement("tr");
    columns.forEach((col) => {
      const th = document.createElement("th");
      th.textContent = col;
      headRow.append(th);
    });
    head.append(headRow);
    const bodyEl = document.createElement("tbody");
    (data.rows || []).forEach((row) => {
      const tr = document.createElement("tr");
      columns.forEach((col) => {
        const td = document.createElement("td");
        const value = row[col];
        if (typeof value === "number") {
          td.className = "num";
          td.textContent = Number.isInteger(value) ? String(value) : value.toFixed(2);
        } else {
          td.textContent = value === null || value === undefined ? "" : String(value);
        }
        tr.append(td);
      });
      bodyEl.append(tr);
    });
    table.append(head, bodyEl);
    byId("result-meta").textContent = "Cube " + data.cube + ", " + data.row_count + " row(s).";
    byId("sql").textContent = data.sql || "";
  }

  async function run(action) {
    try {
      setStatus("Working...");
      renderResult(await action());
      setStatus("");
    } catch (err) {
      setStatus(err.message, true);
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    const tokenInput = byId("token");
    tokenInput.value = sessionStorage.getItem(TOKEN_KEY) || "";

    byId("token-form").addEventListener("submit", (event) => {
      event.preventDefault();
      sessionStorage.setItem(TOKEN_KEY, tokenInput.value.trim());
      loadCubes().catch((err) => setStatus(err.message, true));
    });

    byId("cube").addEventListener("change", (event) => {
      showCube(event.target.value).catch((err) => setStatus(err.message, true));
    });

    byId("plan-form").addEventListener("submit", (event) => {
      event.preventDefault();
      const plan = {
        cube: byId("cube").value,
        dimensions: checked("dimensions"),
        metrics: checked("metrics"),
        limit: Number(byId("limit").value) || 100,
      };
      run(() => api("/api/plan", { plan: plan }));
    });

    byId("ask-form").addEventListener("submit", (event) => {
      event.preventDefault();
      const question = byId("question").value.trim();
      if (!question) return;
      run(() => api("/api/ask", { question: question }));
    });

    loadCubes().catch((err) => setStatus(err.message + " Enter the API token above.", true));
  });
})();
