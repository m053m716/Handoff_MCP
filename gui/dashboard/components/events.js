import { $, escapeHtml, fmtTime, setEmpty, statusBadge } from "../dom.js";

export class EventsPanel {
  constructor({ documentRef = globalThis.document } = {}) {
    this.document = documentRef;
  }

  render(events = []) {
    const rows = Array.isArray(events) ? events : [];
    const count = $("eventCount", this.document);
    const body = $("taskEvents", this.document);
    if (count) count.textContent = `${rows.length} shown`;
    setEmpty("eventsEmpty", rows.length === 0, this.document);
    if (!body) return;
    body.innerHTML = rows.map((event) => `
      <tr>
        <td>${escapeHtml(fmtTime(event.ts || event.created_at || event.timestamp))}</td>
        <td>${escapeHtml(event.action || event.event_type)}</td>
        <td>${statusBadge(event.status)}</td>
        <td>${escapeHtml(event.task_key)}</td>
        <td>${escapeHtml(event.agent_id)}</td>
        <td>${escapeHtml(event.summary || event.detail)}</td>
      </tr>
    `).join("");
  }
}
