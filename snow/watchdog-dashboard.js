/*
 * Watchdog ServiceNow Dashboard
 * -----------------------------
 * Repository: [chmajster/ChrisScriptBase](https://github.com/chmajster/ChrisScriptBase)
 *
 * Run this file inside an authenticated ServiceNow browser session as a
 * bookmarklet, userscript, or browser snippet. It uses the current page's
 * ServiceNow session cookies and does not require a backend server.
 *
 * Customization starts in CONFIG below:
 * - Add group sys_ids to CONFIG.groups.
 * - Tune query fragments in CONFIG.queryFragments.
 * - Enable, disable, or clone report definitions in CONFIG.reports.
 * - Adjust fields per report to match your ServiceNow instance dictionary.
 */
(function watchdogBootstrap() {
  "use strict";

  const CONFIG = {
    instanceUrl: window.location.origin,
    storageKey: "watchdog.dashboard.v2",
    containerId: "watchdog-dashboard-root",
    timezone: undefined,
    keepAliveMs: 8 * 60 * 1000,
    defaultReportIntervalMs: 2 * 60 * 1000,
    preloadHighcharts: true,
    highchartsUrl: "https://code.highcharts.com/highcharts.js",
    notificationIconSize: 96,
    currentUserFallback: "",

    // Put company/team-specific values here only. Do not hard-code them in logic.
    groups: [
      {
        id: "example-team",
        name: "Example Team",
        sysId: "",
        description: "Replace sysId with an assignment group sys_id.",
        enabled: false
      }
    ],

    views: [
      {
        id: "operations",
        name: "Operations",
        reports: [
          "sla-wall",
          "unassigned-incidents",
          "assigned-incidents",
          "waiting-incidents",
          "waiting-3-strike",
          "kanban"
        ]
      },
      {
        id: "catalog",
        name: "Catalog",
        reports: ["unassigned-catalog-tasks", "assigned-catalog-tasks", "eam-manual-access"]
      },
      {
        id: "problem-change",
        name: "Problem & Change",
        reports: [
          "unassigned-problems",
          "assigned-problems",
          "problem-tasks",
          "deployment-change-tasks",
          "pending-approvals"
        ]
      }
    ],

    options: {
      mode: "team",
      viewId: "operations",
      selectedGroups: [],
      selectedReports: [],
      showNotifications: true,
      enableSounds: false,
      showEmptySections: false,
      showContactLinks: true,
      theme: "day",
      accent: "blue"
    },

    queryFragments: {
      active: "active=true",
      incidentOngoing: "active=true^stateNOT IN6,7,8",
      incidentAssigned: "active=true^stateNOT IN6,7,8^assignment_groupIN{groups}",
      incidentUnassigned: "active=true^stateNOT IN6,7,8^assigned_toISEMPTY^assignment_groupIN{groups}",
      incidentWaiting: "active=true^stateNOT IN6,7,8^stateIN3,4",
      incidentColorFiltered: "active=true^stateNOT IN6,7,8^priorityIN1,2",
      catalogOpen: "active=true^stateNOT IN3,4,7",
      problemOpen: "active=true^stateNOT IN107,108",
      problemTaskOpen: "active=true^stateNOT IN3,4,7",
      deploymentOpen: "active=true^stateNOT IN3,4,7",
      approvalsPending: "state=requested",
      activeSla: "active=true^stage!=cancelled"
    },

    commonFields: {
      task: [
        "sys_id",
        "number",
        "short_description",
        "state",
        "priority",
        "assignment_group",
        "assigned_to",
        "opened_at",
        "sys_updated_on",
        "sys_updated_by",
        "due_date",
        "u_due_date",
        "u_planned_end",
        "planned_end_date"
      ],
      user: [
        "sys_id",
        "name",
        "user_name",
        "email",
        "manager",
        "title",
        "department",
        "company",
        "location",
        "employee_number",
        "vip",
        "active"
      ]
    },

    reports: []
  };

  CONFIG.reports = [
    {
      id: "sla-hidden",
      name: "SLA Hidden Data",
      description: "Active task_sla records used to decorate other reports.",
      template: "SlaReport",
      table: "task_sla",
      queryBuilder: (ctx) => ctx.query("activeSla"),
      fields: [
        "sys_id",
        "task",
        "task.number",
        "task.sys_class_name",
        "sla",
        "stage",
        "business_percentage",
        "business_time_left",
        "planned_end_time",
        "has_breached",
        "active"
      ],
      intervalMs: 60 * 1000,
      display: false,
      personable: false,
      showNotifications: false,
      showSLA: false,
      showNextUpdate: false,
      sortBy: "planned_end_time",
      sortDir: "asc"
    },
    {
      id: "sla-wall",
      name: "SLA Wall",
      description: "Every active SLA as a compact progress card.",
      template: "SlaWallReport",
      table: "task_sla",
      queryBuilder: (ctx) => ctx.query("activeSla"),
      fields: [
        "sys_id",
        "task",
        "task.number",
        "task.sys_class_name",
        "sla",
        "stage",
        "business_percentage",
        "business_time_left",
        "planned_end_time",
        "has_breached",
        "active"
      ],
      intervalMs: 60 * 1000,
      display: true,
      personable: false,
      showNotifications: true,
      showSLA: false,
      showNextUpdate: true,
      sortBy: "planned_end_time",
      sortDir: "asc"
    },
    {
      id: "unassigned-incidents",
      name: "Unassigned Ongoing Incidents / Events",
      description: "Active incident/event records without an assignee.",
      template: "IncidentReport",
      table: "incident",
      queryBuilder: (ctx) => ctx.scope(ctx.query("incidentUnassigned")),
      fields: CONFIG.commonFields.task.concat(["category", "subcategory", "contact_type", "caller_id"]),
      intervalMs: 90 * 1000,
      display: true,
      personable: true,
      showNotifications: true,
      showSLA: true,
      showNextUpdate: true,
      sortBy: "priority",
      sortDir: "asc",
      filter: null
    },
    {
      id: "assigned-incidents",
      name: "Assigned Ongoing Incidents / Events",
      description: "Active incident/event records assigned to selected teams or the current user.",
      template: "IncidentReport",
      table: "incident",
      queryBuilder: (ctx) => ctx.scope(ctx.query("incidentAssigned")),
      fields: CONFIG.commonFields.task.concat(["category", "subcategory", "contact_type", "caller_id"]),
      intervalMs: 90 * 1000,
      display: true,
      personable: true,
      showNotifications: true,
      showSLA: true,
      showNextUpdate: true,
      sortBy: "sys_updated_on",
      sortDir: "asc"
    },
    {
      id: "color-filtered-incidents",
      name: "Color-Filtered Incidents / Events",
      description: "Priority incidents/events for color status review.",
      template: "IncidentReport",
      table: "incident",
      queryBuilder: (ctx) => ctx.scope(ctx.query("incidentColorFiltered")),
      fields: CONFIG.commonFields.task.concat(["category", "subcategory", "contact_type", "caller_id"]),
      intervalMs: 90 * 1000,
      display: true,
      personable: true,
      showNotifications: true,
      showSLA: true,
      showNextUpdate: true,
      sortBy: "priority",
      sortDir: "asc"
    },
    {
      id: "waiting-incidents",
      name: "Waiting-For Incidents / Events",
      description: "Incidents/events in waiting states.",
      template: "IncidentReport",
      table: "incident",
      queryBuilder: (ctx) => ctx.scope(ctx.and(ctx.query("incidentAssigned"), ctx.query("incidentWaiting"))),
      fields: CONFIG.commonFields.task.concat(["category", "subcategory", "contact_type", "caller_id"]),
      intervalMs: 2 * 60 * 1000,
      display: true,
      personable: true,
      showNotifications: true,
      showSLA: true,
      showNextUpdate: true,
      sortBy: "sys_updated_on",
      sortDir: "asc"
    },
    {
      id: "waiting-3-strike",
      name: "Waiting-For 3-Strike Candidates",
      description: "Waiting records whose last team update is older than 3 business days.",
      template: "IncidentReport",
      table: "incident",
      queryBuilder: (ctx) => ctx.scope(ctx.and(ctx.query("incidentAssigned"), ctx.query("incidentWaiting"))),
      fields: CONFIG.commonFields.task.concat(["category", "subcategory", "contact_type", "caller_id"]),
      intervalMs: 2 * 60 * 1000,
      display: true,
      personable: true,
      showNotifications: true,
      showSLA: true,
      showNextUpdate: true,
      sortBy: "sys_updated_on",
      sortDir: "asc",
      filter: (record) => record._validation && record._validation.isThreeStrike
    },
    {
      id: "unassigned-catalog-tasks",
      name: "Unassigned Catalog Tasks",
      description: "Open sc_task records without an assignee.",
      template: "CatalogTaskReport",
      table: "sc_task",
      queryBuilder: (ctx) => ctx.scope(ctx.and(ctx.query("catalogOpen"), "assigned_toISEMPTY^assignment_groupIN{groups}")),
      fields: CONFIG.commonFields.task.concat(["request_item", "request_item.cat_item"]),
      intervalMs: 2 * 60 * 1000,
      display: true,
      personable: true,
      showNotifications: true,
      showSLA: true,
      showNextUpdate: true,
      sortBy: "due_date",
      sortDir: "asc"
    },
    {
      id: "assigned-catalog-tasks",
      name: "Assigned Catalog Tasks",
      description: "Open sc_task records assigned to selected teams or the current user.",
      template: "CatalogTaskReport",
      table: "sc_task",
      queryBuilder: (ctx) => ctx.scope(ctx.and(ctx.query("catalogOpen"), "assignment_groupIN{groups}")),
      fields: CONFIG.commonFields.task.concat(["request_item", "request_item.cat_item"]),
      intervalMs: 2 * 60 * 1000,
      display: true,
      personable: true,
      showNotifications: true,
      showSLA: true,
      showNextUpdate: true,
      sortBy: "due_date",
      sortDir: "asc"
    },
    {
      id: "unassigned-problems",
      name: "Unassigned Problems",
      description: "Open problem records without an assignee.",
      template: "ProblemReport",
      table: "problem",
      queryBuilder: (ctx) => ctx.scope(ctx.and(ctx.query("problemOpen"), "assigned_toISEMPTY^assignment_groupIN{groups}")),
      fields: CONFIG.commonFields.task.concat(["known_error", "workaround"]),
      intervalMs: 5 * 60 * 1000,
      display: true,
      personable: true,
      showNotifications: true,
      showSLA: false,
      showNextUpdate: true,
      sortBy: "sys_updated_on",
      sortDir: "asc"
    },
    {
      id: "assigned-problems",
      name: "Assigned Problems",
      description: "Open problem records assigned to selected teams or the current user.",
      template: "ProblemReport",
      table: "problem",
      queryBuilder: (ctx) => ctx.scope(ctx.and(ctx.query("problemOpen"), "assignment_groupIN{groups}")),
      fields: CONFIG.commonFields.task.concat(["known_error", "workaround"]),
      intervalMs: 5 * 60 * 1000,
      display: true,
      personable: true,
      showNotifications: true,
      showSLA: false,
      showNextUpdate: true,
      sortBy: "sys_updated_on",
      sortDir: "asc"
    },
    {
      id: "problem-tasks",
      name: "Problem Tasks",
      description: "Open problem_task records.",
      template: "ProblemReport",
      table: "problem_task",
      queryBuilder: (ctx) => ctx.scope(ctx.and(ctx.query("problemTaskOpen"), "assignment_groupIN{groups}")),
      fields: CONFIG.commonFields.task.concat(["problem"]),
      intervalMs: 5 * 60 * 1000,
      display: true,
      personable: true,
      showNotifications: true,
      showSLA: false,
      showNextUpdate: true,
      sortBy: "sys_updated_on",
      sortDir: "asc"
    },
    {
      id: "external-tasks",
      name: "External Tasks",
      description: "Open tasks with external/vendor-oriented markers. Adjust query for your instance.",
      template: "TableReport",
      table: "task",
      queryBuilder: (ctx) => ctx.scope(ctx.and(ctx.query("active"), "assignment_groupIN{groups}^short_descriptionLIKEexternal")),
      fields: CONFIG.commonFields.task,
      intervalMs: 5 * 60 * 1000,
      display: true,
      personable: true,
      showNotifications: true,
      showSLA: true,
      showNextUpdate: true,
      sortBy: "sys_updated_on",
      sortDir: "asc"
    },
    {
      id: "eam-manual-access",
      name: "EAM / Manual Access Provisioning Tasks",
      description: "Open catalog tasks related to access provisioning. Adjust query for your instance.",
      template: "CatalogTaskReport",
      table: "sc_task",
      queryBuilder: (ctx) => ctx.scope(ctx.and(ctx.query("catalogOpen"), "assignment_groupIN{groups}^short_descriptionLIKEaccess")),
      fields: CONFIG.commonFields.task.concat(["request_item", "request_item.cat_item"]),
      intervalMs: 5 * 60 * 1000,
      display: true,
      personable: true,
      showNotifications: true,
      showSLA: true,
      showNextUpdate: true,
      sortBy: "due_date",
      sortDir: "asc"
    },
    {
      id: "deployment-change-tasks",
      name: "Deployment / Change Tasks",
      description: "Open change_task records.",
      template: "DeploymentTaskReport",
      table: "change_task",
      queryBuilder: (ctx) => ctx.scope(ctx.and(ctx.query("deploymentOpen"), "assignment_groupIN{groups}")),
      fields: CONFIG.commonFields.task.concat(["change_request", "planned_start_date", "planned_end_date"]),
      intervalMs: 3 * 60 * 1000,
      display: true,
      personable: true,
      showNotifications: true,
      showSLA: true,
      showNextUpdate: true,
      sortBy: "planned_end_date",
      sortDir: "asc"
    },
    {
      id: "pending-approvals",
      name: "Pending Approvals",
      description: "Requested sysapproval_approver records.",
      template: "TableReport",
      table: "sysapproval_approver",
      queryBuilder: (ctx) => ctx.scope(ctx.and(ctx.query("approvalsPending"), "approver={user}")),
      fields: [
        "sys_id",
        "sysapproval",
        "approver",
        "state",
        "source_table",
        "document_id",
        "sys_created_on",
        "sys_updated_on"
      ],
      intervalMs: 2 * 60 * 1000,
      display: true,
      personable: false,
      showNotifications: true,
      showSLA: false,
      showNextUpdate: true,
      sortBy: "sys_created_on",
      sortDir: "asc"
    },
    {
      id: "kanban",
      name: "Kanban Workload",
      description: "Incidents, problems, catalog tasks, and deployment tasks grouped by assigned user.",
      template: "KanbanReport",
      table: "",
      queryBuilder: null,
      fields: [],
      intervalMs: 2 * 60 * 1000,
      display: true,
      personable: true,
      showNotifications: false,
      showSLA: true,
      showNextUpdate: true,
      sortBy: "priority",
      sortDir: "asc"
    }
  ];

  const REPORT_TEMPLATES = {};
  const STATUS = {
    green: { label: "OK", color: "#17803d" },
    amber: { label: "Warn", color: "#b76b00" },
    red: { label: "Alert", color: "#c62828" },
    neutral: { label: "Info", color: "#627084" }
  };

  function escapeHtml(value) {
    return String(value ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function cssEscape(value) {
    if (window.CSS && typeof window.CSS.escape === "function") return window.CSS.escape(String(value));
    return String(value).replace(/[^a-zA-Z0-9_-]/g, "\\$&");
  }

  function stableId(prefix, value) {
    return `${prefix}-${String(value || "empty").replace(/[^a-zA-Z0-9_-]/g, "-")}`;
  }

  function uniq(values) {
    return Array.from(new Set(values.filter(Boolean)));
  }

  function compactQuery(parts) {
    return parts.filter(Boolean).join("^").replace(/\^{2,}/g, "^").replace(/^\^|\^$/g, "");
  }

  function fieldValue(record, field) {
    if (!record) return "";
    if (Object.prototype.hasOwnProperty.call(record, field)) return record[field];
    const parts = field.split(".");
    let current = record;
    for (const part of parts) {
      current = current && current[part];
    }
    return current ?? "";
  }

  function displayValue(record, field) {
    return fieldValue(record, `dv_${field}`) || fieldValue(record, field);
  }

  function rawValue(record, field) {
    return fieldValue(record, field);
  }

  function parseDate(value) {
    if (!value) return null;
    if (value instanceof Date) return Number.isNaN(value.getTime()) ? null : value;
    const raw = String(value).trim();
    if (!raw) return null;
    const isoish = raw.includes("T") ? raw : raw.replace(" ", "T");
    const withZone = /(?:Z|[+-]\d\d:?\d\d)$/.test(isoish) ? isoish : `${isoish}Z`;
    const parsed = new Date(withZone);
    return Number.isNaN(parsed.getTime()) ? null : parsed;
  }

  function formatDate(value, multiline = false) {
    const date = parseDate(value);
    if (!date) return "";
    const text = new Intl.DateTimeFormat(undefined, {
      year: "numeric",
      month: "short",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit"
    }).format(date);
    return multiline ? text.replace(",", "<br>") : text;
  }

  function timeAgo(value) {
    const date = parseDate(value);
    if (!date) return "";
    const diffMs = Date.now() - date.getTime();
    const absMs = Math.abs(diffMs);
    const units = [
      ["day", 86400000],
      ["hour", 3600000],
      ["minute", 60000],
      ["second", 1000]
    ];
    for (const [name, size] of units) {
      const amount = Math.floor(absMs / size);
      if (amount >= 1) return `${amount} ${name}${amount === 1 ? "" : "s"} ${diffMs >= 0 ? "ago" : "from now"}`;
    }
    return "just now";
  }

  function businessDaysBetween(start, end) {
    const a = parseDate(start);
    const b = parseDate(end);
    if (!a || !b) return 0;
    const direction = b >= a ? 1 : -1;
    const cursor = new Date(Date.UTC(a.getUTCFullYear(), a.getUTCMonth(), a.getUTCDate()));
    const target = new Date(Date.UTC(b.getUTCFullYear(), b.getUTCMonth(), b.getUTCDate()));
    let days = 0;
    while ((direction === 1 && cursor < target) || (direction === -1 && cursor > target)) {
      cursor.setUTCDate(cursor.getUTCDate() + direction);
      const weekday = cursor.getUTCDay();
      if (weekday !== 0 && weekday !== 6) days += direction;
    }
    return days;
  }

  function addDays(value, days) {
    const date = parseDate(value);
    if (!date) return null;
    const next = new Date(date.getTime());
    next.setUTCDate(next.getUTCDate() + days);
    return next;
  }

  function minutesUntil(value) {
    const date = parseDate(value);
    if (!date) return Number.POSITIVE_INFINITY;
    return Math.round((date.getTime() - Date.now()) / 60000);
  }

  function sortByField(records, field, dir = "asc") {
    const factor = dir === "desc" ? -1 : 1;
    return records.slice().sort((a, b) => {
      const av = displayValue(a, field);
      const bv = displayValue(b, field);
      const ad = parseDate(av);
      const bd = parseDate(bv);
      if (ad && bd) return (ad - bd) * factor;
      const an = Number(av);
      const bn = Number(bv);
      if (!Number.isNaN(an) && !Number.isNaN(bn)) return (an - bn) * factor;
      return String(av).localeCompare(String(bv), undefined, { numeric: true, sensitivity: "base" }) * factor;
    });
  }

  function filterRecords(records, field, operator, value) {
    return records.filter((record) => {
      const actual = displayValue(record, field);
      switch (operator) {
        case "=":
        case "==":
          return String(actual) === String(value);
        case "!=":
          return String(actual) !== String(value);
        case "contains":
          return String(actual).toLowerCase().includes(String(value).toLowerCase());
        case "empty":
          return !actual;
        case "notEmpty":
          return Boolean(actual);
        case ">":
          return Number(actual) > Number(value);
        case "<":
          return Number(actual) < Number(value);
        default:
          return true;
      }
    });
  }

  function normalizeServiceNowResponse(response) {
    const rows = Array.isArray(response && response.result) ? response.result : [];
    return rows.map((row) => normalizeRecord(row));
  }

  function normalizeRecord(row) {
    const normalized = {};
    Object.entries(row || {}).forEach(([key, value]) => {
      if (value && typeof value === "object" && !Array.isArray(value)) {
        normalized[key] = value.value ?? "";
        normalized[`dv_${key}`] = value.display_value ?? value.value ?? "";
        if (value.link) normalized[`link_${key}`] = value.link;
      } else {
        normalized[key] = value ?? "";
        normalized[`dv_${key}`] = value ?? "";
      }
    });
    return normalized;
  }

  function recordNumber(record) {
    return displayValue(record, "number") || displayValue(record, "task.number") || displayValue(record, "sysapproval") || "record";
  }

  function svgNotificationIcon(color, label) {
    const size = CONFIG.notificationIconSize;
    const safeColor = encodeURIComponent(color);
    const safeLabel = encodeURIComponent(String(label || "!").slice(0, 2).toUpperCase());
    const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${size}" height="${size}" viewBox="0 0 ${size} ${size}"><rect width="${size}" height="${size}" rx="18" fill="${safeColor}"/><text x="50%" y="55%" dominant-baseline="middle" text-anchor="middle" font-family="Arial,sans-serif" font-size="42" font-weight="700" fill="white">${safeLabel}</text></svg>`;
    return `data:image/svg+xml;charset=UTF-8,${svg}`;
  }

  function makeButton(label, attrs = {}) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = label;
    Object.entries(attrs).forEach(([key, value]) => {
      if (key === "className") button.className = value;
      else if (key === "dataset") Object.assign(button.dataset, value);
      else button.setAttribute(key, value);
    });
    return button;
  }

  class WatchdogDashboard {
    constructor(config) {
      this.config = config;
      this.state = this.loadState();
      this.reportMap = new Map(config.reports.map((report) => [report.id, report]));
      this.reports = new Map();
      this.slaByTask = new Map();
      this.membersByGroup = new Map();
      this.userMap = new Map();
      this.previousRecordKeys = new Map();
      this.notificationKeys = new Set();
      this.timers = new Set();
      this.currentUserId = this.detectCurrentUser();
      this.currentUserName = this.detectCurrentUserName();
      this.root = null;
      this.main = null;
      this.sidebar = null;
      this.sectionContainer = null;
      this.toastRegion = null;
      this.searchModal = null;
      this.lastRefreshStarted = null;
      this.keydownHandler = null;
    }

    async init() {
      this.installStyles();
      this.buildShell();
      this.bindGlobalEvents();
      await this.loadTeamData();
      await this.loadMissingUserDetails([this.currentUserId]);
      this.mountReports();
      if (this.config.preloadHighcharts) this.loadHighcharts().catch(() => {});
      await this.refreshAll();
      this.startCentralTimer();
      this.startKeepAlive();
    }

    destroy() {
      this.timers.forEach((id) => window.clearInterval(id));
      this.timers.clear();
      if (this.root) this.root.remove();
      if (this.searchModal) this.searchModal.remove();
      if (this.keepAliveFrame) this.keepAliveFrame.remove();
      if (this.keydownHandler) document.removeEventListener("keydown", this.keydownHandler);
    }

    loadState() {
      let saved = {};
      try {
        saved = JSON.parse(localStorage.getItem(this.config.storageKey) || "{}");
      } catch (error) {
        saved = {};
      }
      const merged = { ...this.config.options, ...saved };
      const enabledGroups = this.config.groups.filter((group) => group.enabled && group.sysId).map((group) => group.id);
      if (!merged.selectedGroups || !merged.selectedGroups.length) merged.selectedGroups = enabledGroups;
      const view = this.config.views.find((item) => item.id === merged.viewId) || this.config.views[0];
      if (!merged.selectedReports || !merged.selectedReports.length) merged.selectedReports = view ? view.reports.slice() : [];
      return merged;
    }

    saveState() {
      localStorage.setItem(this.config.storageKey, JSON.stringify(this.state));
      document.documentElement.dataset.watchdogTheme = this.state.theme;
      document.documentElement.dataset.watchdogAccent = this.state.accent;
    }

    detectCurrentUser() {
      return (
        (window.NOW && window.NOW.user && (window.NOW.user.userID || window.NOW.user.sys_id)) ||
        (window.NOW && window.NOW.user_id) ||
        this.config.currentUserFallback ||
        ""
      );
    }

    detectCurrentUserName() {
      return (
        (window.NOW && window.NOW.user && (window.NOW.user.name || window.NOW.user.userName)) ||
        (window.g_user && typeof window.g_user.getFullName === "function" && window.g_user.getFullName()) ||
        ""
      );
    }

    installStyles() {
      if (document.getElementById("watchdog-dashboard-style")) return;
      const style = document.createElement("style");
      style.id = "watchdog-dashboard-style";
      style.textContent = `
        :root {
          --wd-bg: #f6f8fb;
          --wd-panel: #ffffff;
          --wd-panel-soft: #eef3f8;
          --wd-text: #17212f;
          --wd-muted: #66758a;
          --wd-border: #d8e0ea;
          --wd-accent: #1769e0;
          --wd-accent-weak: #dceaff;
          --wd-red: #c62828;
          --wd-amber: #b76b00;
          --wd-green: #17803d;
          --wd-sidebar: #101827;
          --wd-sidebar-text: #e9eef7;
          --wd-shadow: 0 10px 30px rgba(21, 31, 48, 0.12);
          color-scheme: light;
        }
        :root[data-watchdog-theme="night"] {
          --wd-bg: #10141c;
          --wd-panel: #171d27;
          --wd-panel-soft: #202838;
          --wd-text: #e9eef7;
          --wd-muted: #aab5c5;
          --wd-border: #30394a;
          --wd-accent-weak: #182f58;
          --wd-sidebar: #090d14;
          --wd-sidebar-text: #f3f6fb;
          --wd-shadow: 0 12px 34px rgba(0, 0, 0, 0.35);
          color-scheme: dark;
        }
        :root[data-watchdog-accent="red"] {
          --wd-accent: #c62828;
          --wd-accent-weak: #ffe2e2;
        }
        :root[data-watchdog-theme="night"][data-watchdog-accent="red"] {
          --wd-accent-weak: #4a1f24;
        }
        #${this.config.containerId} {
          position: fixed;
          inset: 0;
          z-index: 2147483000;
          display: grid;
          grid-template-columns: 248px minmax(0, 1fr);
          background: var(--wd-bg);
          color: var(--wd-text);
          font: 14px/1.45 Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        }
        .wd-sidebar {
          background: var(--wd-sidebar);
          color: var(--wd-sidebar-text);
          padding: 16px 12px;
          display: flex;
          flex-direction: column;
          gap: 14px;
          overflow-y: auto;
        }
        .wd-brand {
          display: flex;
          align-items: center;
          gap: 10px;
          padding: 6px 8px 12px;
          border-bottom: 1px solid rgba(255,255,255,0.14);
        }
        .wd-brand-mark {
          width: 34px;
          height: 34px;
          border-radius: 8px;
          background: var(--wd-accent);
          display: grid;
          place-items: center;
          font-weight: 800;
          color: #fff;
        }
        .wd-brand-title {
          font-size: 17px;
          font-weight: 750;
        }
        .wd-brand-subtitle {
          color: rgba(233,238,247,0.72);
          font-size: 12px;
        }
        .wd-nav-group {
          display: grid;
          gap: 6px;
        }
        .wd-nav-title {
          color: rgba(233,238,247,0.62);
          text-transform: uppercase;
          letter-spacing: .08em;
          font-size: 11px;
          padding: 0 8px;
        }
        .wd-nav-button,
        .wd-icon-button,
        .wd-button,
        .wd-select,
        .wd-input {
          border: 1px solid var(--wd-border);
          border-radius: 8px;
          background: var(--wd-panel);
          color: var(--wd-text);
          min-height: 34px;
        }
        .wd-nav-button {
          border-color: transparent;
          background: transparent;
          color: var(--wd-sidebar-text);
          text-align: left;
          padding: 9px 10px;
          cursor: pointer;
        }
        .wd-nav-button:hover,
        .wd-nav-button.is-active {
          background: rgba(255,255,255,0.1);
        }
        .wd-main {
          min-width: 0;
          overflow: auto;
          display: flex;
          flex-direction: column;
        }
        .wd-topbar {
          position: sticky;
          top: 0;
          z-index: 5;
          background: rgba(246, 248, 251, 0.94);
          backdrop-filter: blur(12px);
          border-bottom: 1px solid var(--wd-border);
          padding: 12px 18px;
          display: flex;
          align-items: center;
          gap: 12px;
          justify-content: space-between;
        }
        .wd-title-row {
          display: flex;
          align-items: baseline;
          gap: 10px;
          flex-wrap: wrap;
        }
        .wd-title {
          margin: 0;
          font-size: 21px;
          font-weight: 780;
        }
        .wd-meta {
          color: var(--wd-muted);
          font-size: 12px;
        }
        .wd-actions {
          display: flex;
          gap: 8px;
          align-items: center;
          flex-wrap: wrap;
          justify-content: flex-end;
        }
        .wd-button,
        .wd-icon-button {
          padding: 7px 11px;
          cursor: pointer;
          font-weight: 650;
        }
        .wd-icon-button {
          min-width: 34px;
          padding: 6px 8px;
        }
        .wd-button.primary {
          background: var(--wd-accent);
          border-color: var(--wd-accent);
          color: #fff;
        }
        .wd-button:disabled,
        .wd-icon-button:disabled {
          opacity: .58;
          cursor: not-allowed;
        }
        .wd-content {
          padding: 18px;
          display: grid;
          gap: 14px;
        }
        .wd-section {
          background: var(--wd-panel);
          border: 1px solid var(--wd-border);
          border-radius: 8px;
          box-shadow: var(--wd-shadow);
          overflow: hidden;
        }
        .wd-section.is-empty:not(.show-empty) {
          display: none;
        }
        .wd-section.is-minimized .wd-section-body {
          display: none;
        }
        .wd-section-header {
          display: flex;
          gap: 12px;
          align-items: center;
          justify-content: space-between;
          padding: 12px 14px;
          background: var(--wd-panel-soft);
          border-bottom: 1px solid var(--wd-border);
        }
        .wd-section-title {
          display: flex;
          flex-wrap: wrap;
          align-items: center;
          gap: 8px;
          min-width: 0;
        }
        .wd-section-title h2 {
          margin: 0;
          font-size: 16px;
          line-height: 1.2;
        }
        .wd-count {
          display: inline-flex;
          align-items: center;
          justify-content: center;
          min-width: 26px;
          height: 24px;
          padding: 0 8px;
          border-radius: 999px;
          background: var(--wd-accent);
          color: #fff;
          font-size: 12px;
          font-weight: 760;
        }
        .wd-section-controls {
          display: flex;
          align-items: center;
          gap: 7px;
          flex-wrap: wrap;
          justify-content: flex-end;
        }
        .wd-section-body {
          overflow-x: auto;
        }
        .wd-table {
          width: 100%;
          border-collapse: separate;
          border-spacing: 0;
          min-width: 980px;
        }
        .wd-table th,
        .wd-table td {
          padding: 9px 10px;
          border-bottom: 1px solid var(--wd-border);
          vertical-align: top;
          text-align: left;
        }
        .wd-table th {
          position: sticky;
          top: 0;
          z-index: 1;
          background: var(--wd-panel);
          color: var(--wd-muted);
          font-size: 12px;
          cursor: pointer;
          white-space: nowrap;
        }
        .wd-table tr:last-child td {
          border-bottom: 0;
        }
        .wd-table tr.is-new {
          animation: wdPulse 2.6s ease-out;
        }
        @keyframes wdPulse {
          0% { background: var(--wd-accent-weak); }
          100% { background: transparent; }
        }
        .wd-link {
          color: var(--wd-accent);
          text-decoration: none;
          font-weight: 650;
        }
        .wd-link:hover {
          text-decoration: underline;
        }
        .wd-status {
          display: inline-flex;
          align-items: center;
          gap: 5px;
          border-radius: 999px;
          padding: 3px 8px;
          font-size: 12px;
          font-weight: 750;
          white-space: nowrap;
        }
        :root[data-watchdog-theme="night"] .wd-topbar {
          background: rgba(16, 20, 28, 0.94);
        }
        .wd-status.green { background: rgba(23, 128, 61, 0.14); color: var(--wd-green); }
        .wd-status.amber { background: rgba(183, 107, 0, 0.15); color: var(--wd-amber); }
        .wd-status.red { background: rgba(198, 40, 40, 0.14); color: var(--wd-red); }
        .wd-status.neutral { background: rgba(98, 112, 132, 0.14); color: var(--wd-muted); }
        .wd-progress {
          min-width: 130px;
        }
        .wd-progress-track {
          width: 100%;
          height: 8px;
          border-radius: 999px;
          background: var(--wd-border);
          overflow: hidden;
        }
        .wd-progress-bar {
          height: 100%;
          width: 0;
          background: var(--wd-green);
        }
        .wd-progress-bar.amber { background: var(--wd-amber); }
        .wd-progress-bar.red { background: var(--wd-red); }
        .wd-progress-label {
          margin-top: 4px;
          color: var(--wd-muted);
          font-size: 12px;
        }
        .wd-empty,
        .wd-error,
        .wd-help,
        .wd-setup {
          padding: 16px;
          color: var(--wd-muted);
        }
        .wd-error {
          color: var(--wd-red);
          background: rgba(198, 40, 40, 0.08);
        }
        .wd-grid {
          display: grid;
          gap: 14px;
        }
        .wd-form-grid {
          display: grid;
          grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
          gap: 14px;
        }
        .wd-fieldset {
          border: 1px solid var(--wd-border);
          border-radius: 8px;
          padding: 12px;
          display: grid;
          gap: 8px;
        }
        .wd-fieldset legend {
          font-weight: 750;
          padding: 0 6px;
        }
        .wd-check {
          display: flex;
          align-items: center;
          gap: 8px;
          min-height: 28px;
        }
        .wd-select,
        .wd-input {
          padding: 6px 8px;
          width: 100%;
        }
        .wd-user {
          display: grid;
          gap: 2px;
        }
        .wd-user-name {
          font-weight: 700;
        }
        .wd-user-detail {
          color: var(--wd-muted);
          font-size: 12px;
        }
        .wd-sla-wall {
          display: grid;
          grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
          gap: 12px;
          padding: 12px;
        }
        .wd-sla-card,
        .wd-kanban-user {
          border: 1px solid var(--wd-border);
          border-radius: 8px;
          padding: 12px;
          background: var(--wd-panel);
          display: grid;
          gap: 8px;
        }
        .wd-kanban {
          display: grid;
          grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
          gap: 12px;
          padding: 12px;
        }
        .wd-badges {
          display: flex;
          flex-wrap: wrap;
          gap: 6px;
        }
        .wd-badge {
          display: inline-flex;
          align-items: center;
          gap: 5px;
          max-width: 100%;
          border-radius: 999px;
          padding: 4px 8px;
          background: var(--wd-panel-soft);
          border: 1px solid var(--wd-border);
          color: var(--wd-text);
          text-decoration: none;
          font-size: 12px;
        }
        .wd-badge.red { border-color: rgba(198, 40, 40, 0.58); }
        .wd-badge.amber { border-color: rgba(183, 107, 0, 0.58); }
        .wd-badge.green { border-color: rgba(23, 128, 61, 0.58); }
        .wd-search-modal {
          position: fixed;
          inset: 0;
          z-index: 2147483600;
          display: none;
          place-items: start center;
          padding-top: 13vh;
          background: rgba(8, 12, 18, .55);
        }
        .wd-search-modal.is-open {
          display: grid;
        }
        .wd-search-box {
          width: min(680px, calc(100vw - 28px));
          background: var(--wd-panel);
          color: var(--wd-text);
          border: 1px solid var(--wd-border);
          border-radius: 8px;
          box-shadow: var(--wd-shadow);
          padding: 14px;
          display: grid;
          gap: 10px;
        }
        .wd-toast-region {
          position: fixed;
          right: 18px;
          bottom: 18px;
          z-index: 2147483500;
          display: grid;
          gap: 8px;
          width: min(380px, calc(100vw - 36px));
        }
        .wd-toast {
          background: var(--wd-panel);
          color: var(--wd-text);
          border: 1px solid var(--wd-border);
          border-left: 5px solid var(--wd-accent);
          border-radius: 8px;
          padding: 10px 12px;
          box-shadow: var(--wd-shadow);
        }
        .wd-toast.red { border-left-color: var(--wd-red); }
        .wd-toast.amber { border-left-color: var(--wd-amber); }
        .wd-toast.green { border-left-color: var(--wd-green); }
        .wd-tooltip {
          position: fixed;
          z-index: 2147483700;
          max-width: 320px;
          background: #111827;
          color: #fff;
          padding: 8px 10px;
          border-radius: 8px;
          font-size: 12px;
          box-shadow: 0 10px 24px rgba(0,0,0,.22);
          pointer-events: none;
          display: none;
        }
        @media (max-width: 820px) {
          #${this.config.containerId} {
            grid-template-columns: 1fr;
          }
          .wd-sidebar {
            position: sticky;
            top: 0;
            z-index: 10;
            flex-direction: row;
            overflow-x: auto;
            padding: 8px;
          }
          .wd-brand,
          .wd-nav-title {
            display: none;
          }
          .wd-nav-group {
            display: flex;
          }
          .wd-nav-button {
            white-space: nowrap;
          }
          .wd-topbar {
            align-items: flex-start;
            flex-direction: column;
          }
        }
      `;
      document.head.appendChild(style);
    }

    buildShell() {
      document.documentElement.dataset.watchdogTheme = this.state.theme;
      document.documentElement.dataset.watchdogAccent = this.state.accent;
      document.body.innerHTML = "";
      this.root = document.createElement("div");
      this.root.id = this.config.containerId;
      this.root.innerHTML = `
        <aside class="wd-sidebar" aria-label="Watchdog navigation">
          <div class="wd-brand">
            <div class="wd-brand-mark">W</div>
            <div>
              <div class="wd-brand-title">Watchdog</div>
              <div class="wd-brand-subtitle">${escapeHtml(this.currentUserName || this.currentUserId || "ServiceNow")}</div>
            </div>
          </div>
          <div class="wd-nav-group" data-nav="sections">
            <div class="wd-nav-title">Reports</div>
          </div>
          <div class="wd-nav-group">
            <div class="wd-nav-title">Tools</div>
            <button class="wd-nav-button" type="button" data-action="show-help">Help</button>
            <button class="wd-nav-button" type="button" data-action="show-setup">Setup</button>
            <button class="wd-nav-button" type="button" data-action="show-options">Options</button>
            <button class="wd-nav-button" type="button" data-action="clear-notifications">Clear Notifications</button>
            <button class="wd-nav-button" type="button" data-action="reload-all">Reload All</button>
          </div>
        </aside>
        <main class="wd-main">
          <div class="wd-topbar">
            <div class="wd-title-row">
              <h1 class="wd-title">Watchdog Dashboard</h1>
              <span class="wd-meta" data-role="mode-label"></span>
              <span class="wd-meta" data-role="refresh-label"></span>
            </div>
            <div class="wd-actions">
              <button class="wd-button" type="button" data-action="open-search">Search</button>
              <button class="wd-button primary" type="button" data-action="reload-all">Reload All</button>
            </div>
          </div>
          <div class="wd-content" data-role="sections"></div>
        </main>
        <div class="wd-toast-region" aria-live="polite"></div>
        <div class="wd-tooltip" role="tooltip"></div>
      `;
      document.body.appendChild(this.root);
      this.sidebar = this.root.querySelector(".wd-sidebar");
      this.main = this.root.querySelector(".wd-main");
      this.sectionContainer = this.root.querySelector('[data-role="sections"]');
      this.toastRegion = this.root.querySelector(".wd-toast-region");
      this.tooltip = this.root.querySelector(".wd-tooltip");
      this.buildSearchModal();
      this.updateChrome();
      this.root.addEventListener("click", (event) => this.handleClick(event));
      this.root.addEventListener("change", (event) => this.handleChange(event));
      this.root.addEventListener("mouseover", (event) => this.showTooltip(event));
      this.root.addEventListener("mouseout", () => this.hideTooltip());
    }

    buildSearchModal() {
      this.searchModal = document.createElement("div");
      this.searchModal.className = "wd-search-modal";
      this.searchModal.innerHTML = `
        <div class="wd-search-box" role="dialog" aria-modal="true" aria-label="Watchdog search">
          <input class="wd-input" data-role="search-input" type="search" autocomplete="off" placeholder="Search ServiceNow or type gr, ci, g, ep commands">
          <div class="wd-meta">Use Esc to close. Number keys jump to visible report sections.</div>
        </div>
      `;
      document.body.appendChild(this.searchModal);
      this.searchModal.addEventListener("click", (event) => {
        if (event.target === this.searchModal) this.closeSearch();
      });
      this.searchModal.querySelector("input").addEventListener("keydown", (event) => {
        if (event.key === "Enter") {
          event.preventDefault();
          this.runSearchCommand(event.currentTarget.value);
        }
      });
    }

    bindGlobalEvents() {
      this.keydownHandler = (event) => {
        if (event.defaultPrevented) return;
        const active = document.activeElement;
        const isTyping = active && ["INPUT", "TEXTAREA", "SELECT"].includes(active.tagName);
        if (event.key === "Escape" && this.searchModal.classList.contains("is-open")) {
          event.preventDefault();
          this.closeSearch();
          return;
        }
        if (!isTyping && event.key.toLowerCase() === "s") {
          event.preventDefault();
          this.openSearch();
          return;
        }
        if (!isTyping && /^[0-9]$/.test(event.key)) {
          const number = event.key === "0" ? 10 : Number(event.key);
          const sections = Array.from(this.sectionContainer.querySelectorAll(".wd-section:not([hidden])"));
          if (sections[number - 1]) {
            event.preventDefault();
            sections[number - 1].scrollIntoView({ behavior: "smooth", block: "start" });
          }
        }
      };
      document.addEventListener("keydown", this.keydownHandler);
    }

    handleClick(event) {
      const button = event.target.closest("button,a");
      if (!button || !this.root.contains(button)) return;
      const action = button.dataset.action;
      if (!action) return;
      if (action === "reload-all") this.refreshAll();
      if (action === "clear-notifications") this.clearNotifications();
      if (action === "show-help") this.renderHelp();
      if (action === "show-setup") this.renderSetup();
      if (action === "show-options") this.renderOptions();
      if (action === "open-search") this.openSearch();
      if (action === "reload-report") this.refreshReport(button.dataset.reportId);
      if (action === "toggle-report") this.toggleReport(button.dataset.reportId);
      if (action === "jump-section") this.jumpToSection(button.dataset.reportId);
      if (action === "save-setup") this.saveSetupFromUi();
      if (action === "save-options") this.saveOptionsFromUi();
      if (action === "back-dashboard") this.renderDashboard();
    }

    handleChange(event) {
      const target = event.target;
      if (target.matches('[data-role="view-select"]')) {
        const view = this.config.views.find((item) => item.id === target.value);
        if (view) {
          this.sectionContainer.querySelectorAll('[data-setup-report]').forEach((input) => {
            input.checked = view.reports.includes(input.value);
          });
        }
      }
    }

    updateChrome() {
      const modeLabel = this.root.querySelector('[data-role="mode-label"]');
      const refreshLabel = this.root.querySelector('[data-role="refresh-label"]');
      const selectedGroups = this.selectedGroups();
      modeLabel.textContent = this.state.mode === "personal"
        ? `Personal mode: ${this.currentUserName || this.currentUserId || "current user"}`
        : `Team mode: ${selectedGroups.map((group) => group.name).join(", ") || "no groups selected"}`;
      refreshLabel.textContent = this.lastRefreshStarted ? `Last refresh ${timeAgo(this.lastRefreshStarted)}` : "";
      const nav = this.sidebar.querySelector('[data-nav="sections"]');
      const existing = nav.querySelectorAll("button");
      existing.forEach((item) => item.remove());
      this.activeReportConfigs().forEach((report, index) => {
        const button = makeButton(`${index < 9 ? index + 1 : "0"}  ${report.name}`, {
          className: "wd-nav-button",
          dataset: { action: "jump-section", reportId: report.id }
        });
        nav.appendChild(button);
      });
    }

    selectedGroups() {
      const selected = new Set(this.state.selectedGroups || []);
      return this.config.groups.filter((group) => selected.has(group.id) && group.sysId);
    }

    selectedGroupSysIds() {
      return this.selectedGroups().map((group) => group.sysId);
    }

    teamMemberIds() {
      return uniq(Array.from(this.membersByGroup.values()).flat().map((member) => member.userId));
    }

    activeReportConfigs() {
      const selected = new Set(this.state.selectedReports || []);
      return this.config.reports.filter((report) => report.display && selected.has(report.id));
    }

    mountReports() {
      this.sectionContainer.innerHTML = "";
      this.reports.clear();
      this.activeReportConfigs().forEach((config) => {
        const Renderer = REPORT_TEMPLATES[config.template] || TableReport;
        const renderer = new Renderer(this, config);
        this.reports.set(config.id, renderer);
        this.sectionContainer.appendChild(renderer.mount());
      });
      this.updateChrome();
    }

    async loadTeamData() {
      const groups = this.selectedGroups();
      const memberUserIds = [];
      await Promise.all(groups.map(async (group) => {
        try {
          const rows = await this.fetchTable("sys_user_grmember", `group=${group.sysId}`, ["sys_id", "group", "user"], {
            limit: 1000
          });
          const members = rows.map((row) => ({
            groupId: group.id,
            userId: rawValue(row, "user"),
            userName: displayValue(row, "user")
          })).filter((member) => member.userId);
          this.membersByGroup.set(group.id, members);
          memberUserIds.push(...members.map((member) => member.userId));
          members.forEach((member) => {
            this.userMap.set(member.userId, { sys_id: member.userId, name: member.userName });
          });
        } catch (error) {
          this.toast(`Could not load members for ${group.name}: ${error.message}`, "amber");
        }
      }));
      await this.loadMissingUserDetails(uniq(memberUserIds));
    }

    async loadMissingUserDetails(userIds) {
      const missing = uniq(userIds).filter((id) => {
        const existing = this.userMap.get(id);
        return id && !(existing && existing._loaded);
      });
      if (!missing.length) return;
      const chunks = [];
      for (let index = 0; index < missing.length; index += 50) chunks.push(missing.slice(index, index + 50));
      await Promise.all(chunks.map(async (chunk) => {
        const rows = await this.fetchTable("sys_user", `sys_idIN${chunk.join(",")}`, this.config.commonFields.user, { limit: 100 });
        rows.forEach((row) => {
          row._loaded = true;
          this.userMap.set(rawValue(row, "sys_id"), row);
        });
      }));
    }

    queryContext() {
      return {
        mode: this.state.mode,
        groups: this.selectedGroupSysIds(),
        currentUserId: this.currentUserId,
        teamMemberIds: this.teamMemberIds(),
        query: (name) => this.config.queryFragments[name] || "",
        and: (...parts) => compactQuery(parts),
        scope: (query) => this.scopeQuery(query)
      };
    }

    scopeQuery(query) {
      let scoped = String(query || "");
      const groupIds = this.selectedGroupSysIds();
      scoped = scoped.replace(/\{groups\}/g, groupIds.join(","));
      scoped = scoped.replace(/\{user\}/g, this.currentUserId || "");
      if (this.state.mode === "personal" && this.currentUserId) {
        const personal = `assigned_to=${this.currentUserId}`;
        scoped = scoped
          .replace(/assignment_groupIN[^ ^]+/g, "")
          .replace(/assigned_toISEMPTY/g, personal);
        if (!/assigned_to=/.test(scoped) && !/approver=/.test(scoped)) scoped = compactQuery([scoped, personal]);
      }
      return compactQuery([scoped]);
    }

    async refreshAll() {
      this.lastRefreshStarted = new Date();
      this.updateChrome();
      await this.refreshHiddenSla();
      await Promise.all(Array.from(this.reports.values()).map((renderer) => renderer.refresh()));
      this.updateChrome();
    }

    async refreshHiddenSla() {
      const report = this.reportMap.get("sla-hidden");
      if (!report) return;
      try {
        const query = report.queryBuilder(this.queryContext());
        const rows = await this.fetchTable(report.table, query, report.fields, { limit: 500 });
        this.slaByTask.clear();
        rows.forEach((row) => {
          const taskId = rawValue(row, "task");
          if (!taskId) return;
          if (!this.slaByTask.has(taskId)) this.slaByTask.set(taskId, []);
          this.slaByTask.get(taskId).push(row);
        });
      } catch (error) {
        this.toast(`SLA preload failed: ${error.message}`, "amber");
      }
    }

    async refreshReport(reportId) {
      const report = this.reports.get(reportId);
      if (report) await report.refresh(true);
    }

    toggleReport(reportId) {
      const report = this.reports.get(reportId);
      if (!report) return;
      report.section.classList.toggle("is-minimized");
      const button = report.section.querySelector('[data-action="toggle-report"]');
      button.textContent = report.section.classList.contains("is-minimized") ? "+" : "-";
    }

    jumpToSection(reportId) {
      const report = this.reports.get(reportId);
      if (report) report.section.scrollIntoView({ behavior: "smooth", block: "start" });
    }

    startCentralTimer() {
      const id = window.setInterval(() => {
        const now = Date.now();
        Array.from(this.reports.values()).forEach((report) => {
          report.updateCountdown(now);
          if (report.nextRefreshAt && now >= report.nextRefreshAt) report.refresh();
        });
        this.updateChrome();
      }, 1000);
      this.timers.add(id);
    }

    startKeepAlive() {
      const iframe = document.createElement("iframe");
      iframe.title = "Watchdog keep alive";
      iframe.hidden = true;
      iframe.style.display = "none";
      document.body.appendChild(iframe);
      this.keepAliveFrame = iframe;
      const keepAlive = () => {
        const url = `${this.config.instanceUrl}/stats.do?sysparm_watchdog_keepalive=${Date.now()}`;
        iframe.src = url;
      };
      keepAlive();
      const id = window.setInterval(keepAlive, this.config.keepAliveMs);
      this.timers.add(id);
    }

    async fetchTable(table, query, fields, options = {}) {
      return fetchTable(this.config.instanceUrl, table, query, fields, options);
    }

    linkToRecord(table, sysId, label) {
      return linkToRecord(this.config.instanceUrl, table, sysId, label);
    }

    buildRecordKey(report, record) {
      return `${report.id}:${rawValue(record, "sys_id") || recordNumber(record)}`;
    }

    rememberNewRows(report, rows) {
      const keys = rows.map((row) => this.buildRecordKey(report, row));
      const previous = this.previousRecordKeys.get(report.id) || new Set();
      const isFirstLoad = !this.previousRecordKeys.has(report.id);
      this.previousRecordKeys.set(report.id, new Set(keys));
      return new Set(isFirstLoad ? [] : keys.filter((key) => !previous.has(key)));
    }

    async notifyForRows(report, rows, newKeys) {
      if (!this.state.showNotifications || !report.config.showNotifications) return;
      const alerts = rows.filter((row) => {
        const validation = row._validation || {};
        const key = this.buildRecordKey(report.config, row);
        return newKeys.has(key) || validation.status === "red" || validation.status === "amber";
      });
      for (const row of alerts.slice(0, 5)) {
        const validation = row._validation || {};
        const status = validation.status || "neutral";
        const key = `${this.buildRecordKey(report.config, row)}:${status}:${validation.message || ""}`;
        if (this.notificationKeys.has(key)) continue;
        this.notificationKeys.add(key);
        this.showNotification(report.config.name, `${recordNumber(row)} ${validation.message || "updated"}`, status);
      }
    }

    async showNotification(title, body, status = "neutral") {
      this.toast(`${title}: ${body}`, status);
      this.playSound(status);
      if (!("Notification" in window) || !this.state.showNotifications) return;
      if (Notification.permission === "default") {
        try {
          await Notification.requestPermission();
        } catch (error) {
          return;
        }
      }
      if (Notification.permission !== "granted") return;
      const color = (STATUS[status] || STATUS.neutral).color;
      const notification = new Notification(title, {
        body,
        icon: svgNotificationIcon(color, status[0] || "i"),
        tag: `watchdog-${title}-${body}`.slice(0, 128),
        silent: this.state.enableSounds
      });
      window.setTimeout(() => notification.close(), 12000);
    }

    playSound(status) {
      if (!this.state.enableSounds) return;
      const frequencies = { red: 330, amber: 440, green: 660, neutral: 520 };
      try {
        const AudioContext = window.AudioContext || window.webkitAudioContext;
        const context = new AudioContext();
        const oscillator = context.createOscillator();
        const gain = context.createGain();
        oscillator.type = "sine";
        oscillator.frequency.value = frequencies[status] || frequencies.neutral;
        gain.gain.setValueAtTime(0.001, context.currentTime);
        gain.gain.exponentialRampToValueAtTime(0.08, context.currentTime + 0.02);
        gain.gain.exponentialRampToValueAtTime(0.001, context.currentTime + 0.22);
        oscillator.connect(gain);
        gain.connect(context.destination);
        oscillator.start();
        oscillator.stop(context.currentTime + 0.24);
        window.setTimeout(() => context.close(), 500);
      } catch (error) {}
    }

    clearNotifications() {
      this.notificationKeys.clear();
      this.toast("Notification memory cleared.", "green");
    }

    toast(message, status = "neutral") {
      if (!this.toastRegion) return;
      const toast = document.createElement("div");
      toast.className = `wd-toast ${status}`;
      toast.textContent = message;
      this.toastRegion.appendChild(toast);
      window.setTimeout(() => toast.remove(), 7000);
    }

    showTooltip(event) {
      const target = event.target.closest("[data-tooltip]");
      if (!target) return;
      this.tooltip.textContent = target.dataset.tooltip;
      this.tooltip.style.display = "block";
      const rect = target.getBoundingClientRect();
      const tipRect = this.tooltip.getBoundingClientRect();
      const left = Math.min(window.innerWidth - tipRect.width - 12, Math.max(12, rect.left));
      const top = Math.min(window.innerHeight - tipRect.height - 12, rect.bottom + 8);
      this.tooltip.style.left = `${left}px`;
      this.tooltip.style.top = `${top}px`;
    }

    hideTooltip() {
      if (this.tooltip) this.tooltip.style.display = "none";
    }

    openSearch() {
      this.searchModal.classList.add("is-open");
      const input = this.searchModal.querySelector("input");
      input.value = "";
      input.focus();
    }

    closeSearch() {
      this.searchModal.classList.remove("is-open");
    }

    runSearchCommand(input) {
      const text = String(input || "").trim();
      if (!text) return;
      const [command, ...rest] = text.split(/\s+/);
      const query = rest.join(" ").trim();
      const encoded = encodeURIComponent(query || command);
      let url;
      if (command === "gr") url = `${this.config.instanceUrl}/sys_user_grmember_list.do?sysparm_query=user.nameLIKE${encoded}%5EORgroup.nameLIKE${encoded}`;
      else if (command === "ci") url = `${this.config.instanceUrl}/cmdb_ci_list.do?sysparm_query=nameLIKE${encoded}`;
      else if (command === "g") url = `https://www.google.com/search?q=${encoded}`;
      else if (command === "ep") url = `${this.config.instanceUrl}/sys_user_list.do?sysparm_query=nameLIKE${encoded}%5EORemailLIKE${encoded}`;
      else url = `${this.config.instanceUrl}/text_search_exact_match.do?sysparm_search=${encodeURIComponent(text)}`;
      window.open(url, "_blank", "noopener,noreferrer");
      this.closeSearch();
    }

    renderDashboard() {
      this.mountReports();
      this.refreshAll();
    }

    renderHelp() {
      this.sectionContainer.innerHTML = `
        <section class="wd-section show-empty">
          <div class="wd-section-header">
            <div class="wd-section-title"><h2>Help</h2></div>
            <button class="wd-button" type="button" data-action="back-dashboard">Dashboard</button>
          </div>
          <div class="wd-help">
            <p>This dashboard runs entirely in your authenticated ServiceNow browser session.</p>
            <p>Press <strong>S</strong> for search, <strong>Esc</strong> to close search, and <strong>1-9/0</strong> to jump between visible reports.</p>
            <p>Customize groups, query fragments, fields, and reports in the CONFIG section at the top of this file.</p>
          </div>
        </section>
      `;
    }

    renderSetup() {
      const viewOptions = this.config.views.map((view) => `<option value="${escapeHtml(view.id)}" ${view.id === this.state.viewId ? "selected" : ""}>${escapeHtml(view.name)}</option>`).join("");
      const groupChecks = this.config.groups.map((group) => `
        <label class="wd-check">
          <input type="checkbox" data-setup-group value="${escapeHtml(group.id)}" ${this.state.selectedGroups.includes(group.id) ? "checked" : ""} ${group.sysId ? "" : "disabled"}>
          <span>${escapeHtml(group.name)}${group.sysId ? "" : " (add sysId in CONFIG)"}</span>
        </label>
      `).join("");
      const reportChecks = this.config.reports.filter((report) => report.display).map((report) => `
        <label class="wd-check">
          <input type="checkbox" data-setup-report value="${escapeHtml(report.id)}" ${this.state.selectedReports.includes(report.id) ? "checked" : ""}>
          <span>${escapeHtml(report.name)}</span>
        </label>
      `).join("");
      this.sectionContainer.innerHTML = `
        <section class="wd-section show-empty">
          <div class="wd-section-header">
            <div class="wd-section-title"><h2>Setup</h2></div>
            <div class="wd-section-controls">
              <button class="wd-button" type="button" data-action="back-dashboard">Dashboard</button>
              <button class="wd-button primary" type="button" data-action="save-setup">Save</button>
            </div>
          </div>
          <div class="wd-setup wd-grid">
            <div class="wd-form-grid">
              <fieldset class="wd-fieldset">
                <legend>Dashboard View</legend>
                <select class="wd-select" data-role="view-select">${viewOptions}</select>
              </fieldset>
              <fieldset class="wd-fieldset">
                <legend>Mode</legend>
                <label class="wd-check"><input type="radio" name="wd-mode" value="team" ${this.state.mode === "team" ? "checked" : ""}> Team mode</label>
                <label class="wd-check"><input type="radio" name="wd-mode" value="personal" ${this.state.mode === "personal" ? "checked" : ""}> Personal mode</label>
              </fieldset>
            </div>
            <div class="wd-form-grid">
              <fieldset class="wd-fieldset"><legend>Teams</legend>${groupChecks || "<div class='wd-meta'>No groups configured.</div>"}</fieldset>
              <fieldset class="wd-fieldset"><legend>Reports</legend>${reportChecks}</fieldset>
            </div>
          </div>
        </section>
      `;
    }

    renderOptions() {
      const checked = (key) => this.state[key] ? "checked" : "";
      this.sectionContainer.innerHTML = `
        <section class="wd-section show-empty">
          <div class="wd-section-header">
            <div class="wd-section-title"><h2>Options</h2></div>
            <div class="wd-section-controls">
              <button class="wd-button" type="button" data-action="back-dashboard">Dashboard</button>
              <button class="wd-button primary" type="button" data-action="save-options">Save</button>
            </div>
          </div>
          <div class="wd-setup wd-form-grid">
            <fieldset class="wd-fieldset">
              <legend>Alerts</legend>
              <label class="wd-check"><input type="checkbox" data-option="showNotifications" ${checked("showNotifications")}> Browser notifications</label>
              <label class="wd-check"><input type="checkbox" data-option="enableSounds" ${checked("enableSounds")}> Alert sounds</label>
            </fieldset>
            <fieldset class="wd-fieldset">
              <legend>Display</legend>
              <label class="wd-check"><input type="checkbox" data-option="showEmptySections" ${checked("showEmptySections")}> Show empty sections</label>
              <label class="wd-check"><input type="checkbox" data-option="showContactLinks" ${checked("showContactLinks")}> User contact links</label>
            </fieldset>
            <fieldset class="wd-fieldset">
              <legend>Theme</legend>
              <label class="wd-check"><input type="radio" name="wd-theme" value="day" ${this.state.theme === "day" ? "checked" : ""}> Day</label>
              <label class="wd-check"><input type="radio" name="wd-theme" value="night" ${this.state.theme === "night" ? "checked" : ""}> Night</label>
            </fieldset>
            <fieldset class="wd-fieldset">
              <legend>Accent</legend>
              <label class="wd-check"><input type="radio" name="wd-accent" value="blue" ${this.state.accent === "blue" ? "checked" : ""}> Blue</label>
              <label class="wd-check"><input type="radio" name="wd-accent" value="red" ${this.state.accent === "red" ? "checked" : ""}> Red</label>
            </fieldset>
          </div>
        </section>
      `;
    }

    async saveSetupFromUi() {
      this.state.viewId = this.sectionContainer.querySelector('[data-role="view-select"]').value;
      this.state.mode = this.sectionContainer.querySelector('input[name="wd-mode"]:checked').value;
      this.state.selectedGroups = Array.from(this.sectionContainer.querySelectorAll("[data-setup-group]:checked")).map((input) => input.value);
      this.state.selectedReports = Array.from(this.sectionContainer.querySelectorAll("[data-setup-report]:checked")).map((input) => input.value);
      this.saveState();
      this.membersByGroup.clear();
      await this.loadTeamData();
      this.renderDashboard();
    }

    saveOptionsFromUi() {
      this.sectionContainer.querySelectorAll("[data-option]").forEach((input) => {
        this.state[input.dataset.option] = input.checked;
      });
      this.state.theme = this.sectionContainer.querySelector('input[name="wd-theme"]:checked').value;
      this.state.accent = this.sectionContainer.querySelector('input[name="wd-accent"]:checked').value;
      this.saveState();
      this.renderDashboard();
    }

    async loadHighcharts() {
      if (window.Highcharts) return window.Highcharts;
      await new Promise((resolve, reject) => {
        const script = document.createElement("script");
        script.src = this.config.highchartsUrl;
        script.async = true;
        script.onload = resolve;
        script.onerror = reject;
        document.head.appendChild(script);
      });
      return window.Highcharts;
    }
  }

  class TableReport {
    constructor(app, config) {
      this.app = app;
      this.config = config;
      this.section = null;
      this.body = null;
      this.countEl = null;
      this.countdownEl = null;
      this.errorEl = null;
      this.records = [];
      this.sortBy = config.sortBy || "number";
      this.sortDir = config.sortDir || "asc";
      this.nextRefreshAt = 0;
      this.isLoading = false;
    }

    mount() {
      this.section = document.createElement("section");
      this.section.className = `wd-section ${this.app.state.showEmptySections ? "show-empty" : ""}`;
      this.section.id = stableId("wd-report", this.config.id);
      this.section.innerHTML = `
        <div class="wd-section-header">
          <div class="wd-section-title">
            <h2>${escapeHtml(this.config.name)}</h2>
            <span class="wd-count" data-role="count">0</span>
            <span class="wd-meta">${escapeHtml(this.config.description || "")}</span>
          </div>
          <div class="wd-section-controls">
            <span class="wd-meta" data-role="countdown"></span>
            <button class="wd-icon-button" type="button" data-action="reload-report" data-report-id="${escapeHtml(this.config.id)}" data-tooltip="Reload this report">R</button>
            <button class="wd-icon-button" type="button" data-action="toggle-report" data-report-id="${escapeHtml(this.config.id)}" data-tooltip="Minimize or maximize">-</button>
          </div>
        </div>
        <div class="wd-section-body" data-role="body"></div>
      `;
      this.body = this.section.querySelector('[data-role="body"]');
      this.countEl = this.section.querySelector('[data-role="count"]');
      this.countdownEl = this.section.querySelector('[data-role="countdown"]');
      return this.section;
    }

    async refresh(force = false) {
      if (this.isLoading && !force) return;
      this.isLoading = true;
      this.setError("");
      try {
        let rows = await this.loadRecords();
        rows = await this.postProcess(rows);
        if (typeof this.config.filter === "function") rows = rows.filter(this.config.filter);
        rows = sortByField(rows, this.sortBy, this.sortDir);
        const newKeys = this.app.rememberNewRows(this.config, rows);
        this.records = rows;
        this.render(rows, newKeys);
        await this.app.notifyForRows(this, rows, newKeys);
      } catch (error) {
        this.setError(error.message || String(error));
      } finally {
        this.isLoading = false;
        this.nextRefreshAt = Date.now() + (this.config.intervalMs || this.app.config.defaultReportIntervalMs);
        this.updateCountdown(Date.now());
      }
    }

    async loadRecords() {
      if (!this.config.table) return [];
      const query = this.config.queryBuilder ? this.config.queryBuilder(this.app.queryContext()) : "";
      return this.app.fetchTable(this.config.table, query, this.config.fields, { limit: 500 });
    }

    async postProcess(rows) {
      rows.forEach((row) => {
        row._validation = this.validate(row);
      });
      const userIds = uniq(rows.flatMap((row) => [rawValue(row, "assigned_to"), rawValue(row, "opened_by"), rawValue(row, "caller_id")]));
      await this.app.loadMissingUserDetails(userIds);
      return rows;
    }

    validate() {
      return { status: "neutral", message: "" };
    }

    render(rows, newKeys) {
      this.countEl.textContent = String(rows.length);
      this.section.classList.toggle("is-empty", rows.length === 0);
      if (!rows.length) {
        this.body.innerHTML = `<div class="wd-empty">No records found.</div>`;
        return;
      }
      const columns = this.columns();
      this.body.innerHTML = `
        <table class="wd-table">
          <thead><tr>${columns.map((column) => this.renderHeader(column)).join("")}</tr></thead>
          <tbody>${rows.map((row) => this.renderRow(row, columns, newKeys)).join("")}</tbody>
        </table>
      `;
      this.body.querySelectorAll("th[data-field]").forEach((th) => {
        th.addEventListener("click", () => {
          const field = th.dataset.field;
          if (this.sortBy === field) this.sortDir = this.sortDir === "asc" ? "desc" : "asc";
          else {
            this.sortBy = field;
            this.sortDir = "asc";
          }
          this.records = sortByField(this.records, this.sortBy, this.sortDir);
          this.render(this.records, new Set());
        });
      });
    }

    renderHeader(column) {
      const active = this.sortBy === column.field ? ` ${this.sortDir === "asc" ? "up" : "down"}` : "";
      return `<th data-field="${escapeHtml(column.field)}">${escapeHtml(column.label)}${escapeHtml(active)}</th>`;
    }

    renderRow(row, columns, newKeys) {
      const key = this.app.buildRecordKey(this.config, row);
      return `<tr class="${newKeys.has(key) ? "is-new" : ""}">${columns.map((column) => `<td>${this.renderCell(row, column)}</td>`).join("")}</tr>`;
    }

    renderCell(row, column) {
      if (column.render) return column.render(row, this);
      const value = displayValue(row, column.field);
      if (column.type === "date") return formatDate(value, true) || "";
      if (column.type === "user") return this.renderUser(rawValue(row, column.field), displayValue(row, column.field));
      return escapeHtml(value);
    }

    columns() {
      return [
        { label: "Status", field: "_validation.status", render: (row) => this.renderStatus(row) },
        { label: "Number", field: "number", render: (row) => this.renderRecordLink(row) },
        { label: "Summary", field: "short_description" },
        { label: "Priority", field: "priority" },
        { label: "State", field: "state" },
        { label: "Assigned To", field: "assigned_to", type: "user" },
        { label: "Updated", field: "sys_updated_on", type: "date" },
        { label: "SLA", field: "_sla", render: (row) => this.renderSla(row) }
      ].filter((column) => this.config.showSLA || column.field !== "_sla");
    }

    renderStatus(row) {
      const validation = row._validation || { status: "neutral", message: "" };
      const status = validation.status || "neutral";
      const statusInfo = STATUS[status] || STATUS.neutral;
      return `<span class="wd-status ${escapeHtml(status)}" data-tooltip="${escapeHtml(validation.message || statusInfo.label || "")}">${escapeHtml(statusInfo.label || status)}</span>`;
    }

    renderRecordLink(row) {
      return this.app.linkToRecord(this.config.table || displayValue(row, "sys_class_name") || "task", rawValue(row, "sys_id"), recordNumber(row));
    }

    renderUser(userId, fallbackName) {
      if (!userId) return `<span class="wd-meta">${escapeHtml(fallbackName || "Unassigned")}</span>`;
      const user = this.app.userMap.get(userId) || { name: fallbackName || userId };
      const name = displayValue(user, "name") || user.name || fallbackName || userId;
      const manager = displayValue(user, "manager");
      const title = displayValue(user, "title");
      const org = [displayValue(user, "department"), displayValue(user, "company"), displayValue(user, "location")].filter(Boolean).join(" / ");
      const email = displayValue(user, "email");
      const profile = this.app.linkToRecord("sys_user", userId, name);
      const contact = this.app.state.showContactLinks && email
        ? ` <a class="wd-link" href="mailto:${escapeHtml(email)}">Email</a> <a class="wd-link" href="sip:${escapeHtml(email)}">SIP</a>`
        : "";
      const tooltip = [name, title, manager ? `Manager: ${manager}` : "", org, email].filter(Boolean).join("\n");
      return `
        <div class="wd-user" data-tooltip="${escapeHtml(tooltip)}">
          <div class="wd-user-name">${profile}${contact}</div>
          <div class="wd-user-detail">${escapeHtml([title, org].filter(Boolean).join(" - "))}</div>
        </div>
      `;
    }

    renderSla(row) {
      const slas = this.app.slaByTask.get(rawValue(row, "sys_id")) || [];
      if (!slas.length) return `<span class="wd-meta">No active SLA</span>`;
      return slas.slice(0, 2).map((sla) => renderSlaProgress(sla, this.app, true)).join("");
    }

    setError(message) {
      if (!message) {
        if (this.errorEl) this.errorEl.remove();
        this.errorEl = null;
        return;
      }
      this.body.innerHTML = `<div class="wd-error">${escapeHtml(message)}</div>`;
    }

    updateCountdown(now) {
      if (!this.countdownEl) return;
      if (!this.nextRefreshAt) {
        this.countdownEl.textContent = "";
        return;
      }
      const seconds = Math.max(0, Math.ceil((this.nextRefreshAt - now) / 1000));
      this.countdownEl.textContent = this.config.showNextUpdate ? `Next refresh ${seconds}s` : "";
    }
  }

  class IncidentReport extends TableReport {
    validate(row) {
      const opened = parseDate(rawValue(row, "opened_at"));
      const updated = parseDate(rawValue(row, "sys_updated_on"));
      const ageDays = opened ? Math.floor((Date.now() - opened.getTime()) / 86400000) : 0;
      const updateBusinessDays = updated ? businessDaysBetween(updated, new Date()) : 0;
      const updatedBy = String(rawValue(row, "sys_updated_by") || displayValue(row, "sys_updated_by"));
      const teamNames = new Set(this.app.teamMemberIds().map((id) => {
        const user = this.app.userMap.get(id);
        return String(displayValue(user, "user_name") || displayValue(user, "name") || "").toLowerCase();
      }).filter(Boolean));
      const byTeam = teamNames.has(updatedBy.toLowerCase());
      const validation = { status: "green", message: "Current", isThreeStrike: false, needsAcknowledgement: false };
      if (ageDays >= 30) return { status: "red", message: "Opened 30+ days ago", isThreeStrike: false, needsAcknowledgement: false };
      if (ageDays >= 25) validation.status = "amber", validation.message = "Opened 25-30 days ago";
      if (updateBusinessDays > 3 && byTeam) {
        validation.status = severityMax(validation.status, "amber");
        validation.message = "Last team update is older than 3 business days";
        validation.isThreeStrike = true;
      } else if (updateBusinessDays > 3 && !byTeam) {
        validation.status = "red";
        validation.message = "Needs acknowledgement; non-team update older than 3 business days";
        validation.needsAcknowledgement = true;
      } else if (!byTeam && updatedBy) {
        validation.status = severityMax(validation.status, "amber");
        validation.message = "Recent non-team update";
      }
      return validation;
    }
  }

  class ProblemReport extends TableReport {
    validate(row) {
      const updated = rawValue(row, "sys_updated_on");
      const next = addDays(updated, 14);
      if (!next) return { status: "neutral", message: "" };
      const days = Math.ceil((next.getTime() - Date.now()) / 86400000);
      row._nextUpdate = next;
      if (days < 0) return { status: "red", message: "Next update is past due" };
      if (days <= 3) return { status: "amber", message: "Next update is due within 3 days" };
      return { status: "green", message: `Next update ${timeAgo(next)}` };
    }

    columns() {
      const columns = super.columns();
      columns.splice(6, 0, {
        label: "Next Update",
        field: "_nextUpdate",
        render: (row) => formatDate(row._nextUpdate, true)
      });
      return columns;
    }
  }

  class CatalogTaskReport extends TableReport {
    validate(row) {
      const due = rawValue(row, "due_date") || rawValue(row, "u_due_date");
      const dueDate = parseDate(due);
      const updated = rawValue(row, "sys_updated_on");
      const updateBusinessDays = updated ? businessDaysBetween(updated, new Date()) : 0;
      if (dueDate && dueDate.getTime() < Date.now()) {
        row._overdueBucket = `${Math.abs(Math.floor((Date.now() - dueDate.getTime()) / 86400000))} days overdue`;
        return { status: "red", message: `Due date is past (${row._overdueBucket})` };
      }
      if (updateBusinessDays > 3) return { status: "amber", message: "Last update older than 3 business days" };
      return { status: "green", message: "Current" };
    }

    columns() {
      return [
        { label: "Status", field: "_validation.status", render: (row) => this.renderStatus(row) },
        { label: "Number", field: "number", render: (row) => this.renderRecordLink(row) },
        { label: "Summary", field: "short_description" },
        { label: "State", field: "state" },
        { label: "Assigned To", field: "assigned_to", type: "user" },
        { label: "Due", field: "due_date", type: "date" },
        { label: "Progress", field: "_progress", render: (row) => this.renderDueProgress(row) },
        { label: "Updated", field: "sys_updated_on", type: "date" },
        { label: "SLA", field: "_sla", render: (row) => this.renderSla(row) }
      ].filter((column) => this.config.showSLA || column.field !== "_sla");
    }

    renderDueProgress(row) {
      const opened = parseDate(rawValue(row, "opened_at"));
      const due = parseDate(rawValue(row, "due_date") || rawValue(row, "u_due_date"));
      if (!opened || !due) return "";
      if (due.getTime() <= Date.now()) return `<span class="wd-status red">${escapeHtml(row._overdueBucket || "Overdue")}</span>`;
      const total = Math.max(1, due.getTime() - opened.getTime());
      const elapsed = Math.max(0, Date.now() - opened.getTime());
      const percent = Math.min(100, Math.round((elapsed / total) * 100));
      const status = percent > 85 ? "amber" : "green";
      return renderProgress(percent, status, `${percent}% to due date`);
    }
  }

  class DeploymentTaskReport extends TableReport {
    validate(row) {
      const plannedEnd = rawValue(row, "planned_end_date") || rawValue(row, "u_planned_end");
      const mins = minutesUntil(plannedEnd);
      const opened = parseDate(rawValue(row, "opened_at"));
      const updated = parseDate(rawValue(row, "sys_updated_on"));
      const openedDays = opened ? Math.floor((Date.now() - opened.getTime()) / 86400000) : 0;
      const updatedDays = updated ? businessDaysBetween(updated, new Date()) : 0;
      const state = String(displayValue(row, "state")).toLowerCase();
      if (state.includes("open") || state.includes("ready")) return { status: "green", message: "Ready/open task" };
      if (Number.isFinite(mins) && mins <= 0) return { status: "red", message: "Planned end is past" };
      if (Number.isFinite(mins) && mins <= 120) return { status: "amber", message: "Planned end is near" };
      if (openedDays > 14 || updatedDays > 5) return { status: "amber", message: "Task may be stale" };
      return { status: "green", message: "Current" };
    }
  }

  class SlaReport extends TableReport {
    validate(row) {
      return validateSla(row);
    }
  }

  class SlaWallReport extends SlaReport {
    columns() {
      return [];
    }

    render(rows) {
      this.countEl.textContent = String(rows.length);
      this.section.classList.toggle("is-empty", rows.length === 0);
      if (!rows.length) {
        this.body.innerHTML = `<div class="wd-empty">No active SLAs.</div>`;
        return;
      }
      this.body.innerHTML = `<div class="wd-sla-wall">${rows.map((row) => this.renderCard(row)).join("")}</div>`;
    }

    renderCard(row) {
      const taskTable = displayValue(row, "task.sys_class_name") || "task";
      const taskId = rawValue(row, "task");
      const taskNumber = displayValue(row, "task.number") || displayValue(row, "task");
      return `
        <article class="wd-sla-card">
          <div>${this.app.linkToRecord(taskTable, taskId, taskNumber)}</div>
          <div class="wd-meta">${escapeHtml(displayValue(row, "sla"))}</div>
          ${this.renderStatus(row)}
          ${renderSlaProgress(row, this.app, false)}
        </article>
      `;
    }
  }

  class KanbanReport extends TableReport {
    async loadRecords() {
      const ctx = this.app.queryContext();
      const definitions = [
        {
          table: "incident",
          type: "INC",
          query: ctx.scope(ctx.query("incidentAssigned")),
          fields: CONFIG.commonFields.task.concat(["category", "subcategory"])
        },
        {
          table: "problem",
          type: "PRB",
          query: ctx.scope(ctx.and(ctx.query("problemOpen"), "assignment_groupIN{groups}")),
          fields: CONFIG.commonFields.task
        },
        {
          table: "sc_task",
          type: "SCTASK",
          query: ctx.scope(ctx.and(ctx.query("catalogOpen"), "assignment_groupIN{groups}")),
          fields: CONFIG.commonFields.task
        },
        {
          table: "change_task",
          type: "CHG",
          query: ctx.scope(ctx.and(ctx.query("deploymentOpen"), "assignment_groupIN{groups}")),
          fields: CONFIG.commonFields.task
        }
      ];
      const batches = await Promise.all(definitions.map(async (definition) => {
        const rows = await this.app.fetchTable(definition.table, definition.query, definition.fields, { limit: 300 });
        rows.forEach((row) => {
          row._table = definition.table;
          row._type = definition.type;
          row._validation = this.validateByType(row, definition.type);
        });
        return rows;
      }));
      return batches.flat();
    }

    validateByType(row, type) {
      if (type === "INC") return IncidentReport.prototype.validate.call(this, row);
      if (type === "PRB") return ProblemReport.prototype.validate.call(this, row);
      if (type === "SCTASK") return CatalogTaskReport.prototype.validate.call(this, row);
      if (type === "CHG") return DeploymentTaskReport.prototype.validate.call(this, row);
      return { status: "neutral", message: "" };
    }

    render(rows) {
      this.countEl.textContent = String(rows.length);
      this.section.classList.toggle("is-empty", rows.length === 0);
      const groups = this.groupRecords(rows);
      this.body.innerHTML = `<div class="wd-kanban">${groups.map((group) => this.renderUserColumn(group)).join("")}</div>`;
    }

    groupRecords(rows) {
      const map = new Map();
      const addGroup = (id, name) => {
        if (!map.has(id)) map.set(id, { id, name, records: [] });
      };
      this.app.teamMemberIds().forEach((id) => {
        const user = this.app.userMap.get(id);
        addGroup(id, displayValue(user, "name") || id);
      });
      addGroup("UNASSIGNED", "Unassigned");
      addGroup("NONTEAMMEMBER", "Non-team member");
      const team = new Set(this.app.teamMemberIds());
      rows.forEach((row) => {
        const assigned = rawValue(row, "assigned_to");
        const bucket = !assigned ? "UNASSIGNED" : team.has(assigned) ? assigned : "NONTEAMMEMBER";
        addGroup(bucket, bucket === "NONTEAMMEMBER" ? "Non-team member" : displayValue(row, "assigned_to"));
        map.get(bucket).records.push(row);
      });
      return Array.from(map.values()).filter((group) => this.app.state.showEmptySections || group.records.length);
    }

    renderUserColumn(group) {
      const red = group.records.filter((row) => row._validation && row._validation.status === "red").length;
      const amber = group.records.filter((row) => row._validation && row._validation.status === "amber").length;
      return `
        <article class="wd-kanban-user">
          <div>
            <strong>${escapeHtml(group.name)}</strong>
            <span class="wd-meta">${group.records.length} items</span>
            ${red ? `<span class="wd-status red">${red}</span>` : ""}
            ${amber ? `<span class="wd-status amber">${amber}</span>` : ""}
          </div>
          <div class="wd-badges">${group.records.map((row) => this.renderBadge(row)).join("") || "<span class='wd-meta'>No work</span>"}</div>
        </article>
      `;
    }

    renderBadge(row) {
      const status = row._validation && row._validation.status ? row._validation.status : "neutral";
      const table = row._table || "task";
      const sysId = rawValue(row, "sys_id");
      const label = `${row._type || ""} ${recordNumber(row)}`.trim();
      const href = `${this.app.config.instanceUrl}/${encodeURIComponent(table)}.do?sys_id=${encodeURIComponent(sysId)}`;
      const tooltip = [displayValue(row, "short_description"), row._validation && row._validation.message].filter(Boolean).join("\n");
      return `<a class="wd-badge ${escapeHtml(status)}" href="${href}" target="_blank" rel="noopener noreferrer" data-tooltip="${escapeHtml(tooltip)}">${escapeHtml(label)}</a>`;
    }
  }

  REPORT_TEMPLATES.TableReport = TableReport;
  REPORT_TEMPLATES.IncidentReport = IncidentReport;
  REPORT_TEMPLATES.ProblemReport = ProblemReport;
  REPORT_TEMPLATES.CatalogTaskReport = CatalogTaskReport;
  REPORT_TEMPLATES.DeploymentTaskReport = DeploymentTaskReport;
  REPORT_TEMPLATES.SlaReport = SlaReport;
  REPORT_TEMPLATES.SlaWallReport = SlaWallReport;
  REPORT_TEMPLATES.KanbanReport = KanbanReport;

  function severityMax(a, b) {
    const order = { neutral: 0, green: 1, amber: 2, red: 3 };
    return order[b] > order[a] ? b : a;
  }

  function validateSla(row) {
    const breached = String(rawValue(row, "has_breached")) === "true";
    const mins = minutesUntil(rawValue(row, "planned_end_time"));
    if (breached || mins <= 5) return { status: "red", message: breached ? "SLA breached" : "SLA target below 5 minutes" };
    if (mins <= 15) return { status: "amber", message: "SLA target below 15 minutes" };
    return { status: "green", message: "SLA within target" };
  }

  function renderSlaProgress(row, app, compact) {
    const validation = row._validation || validateSla(row);
    const percent = Math.min(100, Math.max(0, Math.round(Number(rawValue(row, "business_percentage") || 0))));
    const label = [
      compact ? "" : displayValue(row, "stage"),
      displayValue(row, "business_time_left") || (Number.isFinite(minutesUntil(rawValue(row, "planned_end_time"))) ? `${minutesUntil(rawValue(row, "planned_end_time"))} min left` : "")
    ].filter(Boolean).join(" - ");
    return renderProgress(percent, validation.status, label || `${percent}% elapsed`);
  }

  function renderProgress(percent, status, label) {
    return `
      <div class="wd-progress">
        <div class="wd-progress-track"><div class="wd-progress-bar ${escapeHtml(status)}" style="width:${Math.max(0, Math.min(100, percent))}%"></div></div>
        <div class="wd-progress-label">${escapeHtml(label)}</div>
      </div>
    `;
  }

  async function fetchTable(instanceUrl, table, query, fields, options = {}) {
    if (!table) return [];
    const params = new URLSearchParams();
    if (query) params.set("sysparm_query", query);
    if (fields && fields.length) params.set("sysparm_fields", uniq(fields).join(","));
    params.set("sysparm_display_value", options.displayValue || "all");
    params.set("sysparm_exclude_reference_link", "true");
    params.set("sysparm_limit", String(options.limit || 500));
    if (options.offset) params.set("sysparm_offset", String(options.offset));
    const url = `${instanceUrl.replace(/\/$/, "")}/api/now/table/${encodeURIComponent(table)}?${params.toString()}`;
    const response = await fetch(url, {
      method: "GET",
      credentials: "include",
      headers: {
        Accept: "application/json",
        "X-UserToken": window.g_ck || ""
      }
    });
    if (!response.ok) {
      const text = await response.text().catch(() => "");
      throw new Error(`${table} ${response.status}: ${text || response.statusText}`);
    }
    const json = await response.json();
    return normalizeServiceNowResponse(json);
  }

  function linkToRecord(instanceUrl, table, sysId, label) {
    const safeLabel = escapeHtml(label || sysId || "record");
    if (!sysId) return safeLabel;
    const href = `${instanceUrl.replace(/\/$/, "")}/${encodeURIComponent(table)}.do?sys_id=${encodeURIComponent(sysId)}`;
    return `<a class="wd-link" href="${href}" target="_blank" rel="noopener noreferrer">${safeLabel}</a>`;
  }

  let activeWatchdogApp = null;

  function showTooltip(anchor, message) {
    if (!activeWatchdogApp || !activeWatchdogApp.tooltip || !anchor) return;
    const tooltip = activeWatchdogApp.tooltip;
    tooltip.textContent = String(message || anchor.getAttribute("title") || anchor.dataset.tooltip || "");
    if (!tooltip.textContent) return;
    tooltip.style.display = "block";
    const rect = anchor.getBoundingClientRect();
    const tipRect = tooltip.getBoundingClientRect();
    tooltip.style.left = `${Math.min(window.innerWidth - tipRect.width - 12, Math.max(12, rect.left))}px`;
    tooltip.style.top = `${Math.min(window.innerHeight - tipRect.height - 12, rect.bottom + 8)}px`;
  }

  function showNotification(title, body, status = "neutral") {
    if (activeWatchdogApp) return activeWatchdogApp.showNotification(title, body, status);
    if ("Notification" in window && Notification.permission === "granted") {
      return new Notification(title, { body });
    }
    return undefined;
  }

  window.WatchdogDashboard = WatchdogDashboard;
  window.WatchdogDashboardConfig = CONFIG;
  window.WatchdogUtils = {
    fetchTable: (table, query, fields, options) => fetchTable(CONFIG.instanceUrl, table, query, fields, options),
    normalizeServiceNowResponse,
    parseDate,
    formatDate,
    timeAgo,
    businessDaysBetween,
    sortByField,
    filterRecords,
    linkToRecord: (table, sysId, label) => linkToRecord(CONFIG.instanceUrl, table, sysId, label),
    escapeHtml,
    showTooltip,
    showNotification
  };

  if (window.__WatchdogDashboardApp && typeof window.__WatchdogDashboardApp.destroy === "function") {
    window.__WatchdogDashboardApp.destroy();
  }
  const app = new WatchdogDashboard(CONFIG);
  window.__WatchdogDashboardApp = app;
  activeWatchdogApp = app;
  app.init().catch((error) => {
    console.error("Watchdog dashboard failed to start", error);
    const fallback = document.createElement("pre");
    fallback.style.cssText = "position:fixed;inset:16px;z-index:2147483647;background:#fff;color:#b00020;padding:16px;white-space:pre-wrap;";
    fallback.textContent = `Watchdog dashboard failed to start:\n${error.stack || error.message || error}`;
    document.body.appendChild(fallback);
  });
})();
