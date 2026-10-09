const state = {
  data: null,
  source: "all",
  category: "all",
  query: "",
  sectionsExpanded: true,
};

const els = {
  title: document.getElementById("page-title"),
  meta: document.getElementById("page-meta"),
  summary: document.getElementById("summary"),
  sourceFilters: document.getElementById("source-filters"),
  status: document.getElementById("data-status"),
  filters: document.getElementById("filters"),
  list: document.getElementById("paper-list"),
  search: document.getElementById("search"),
  refresh: document.getElementById("refresh"),
};

const CATEGORY_LABELS_ZH = {
  "prb": "Physical Review B",
  "cond-mat.dis-nn": "无序系统与神经网络",
  "cond-mat.mes-hall": "介观系统与量子霍尔",
  "cond-mat.mtrl-sci": "材料科学",
  "cond-mat.other": "凝聚态其他",
  "cond-mat.quant-gas": "量子气体",
  "cond-mat.soft": "软凝聚态物质",
  "cond-mat.stat-mech": "统计力学",
  "cond-mat.str-el": "强关联电子系统",
  "cond-mat.supr-con": "超导电性",
};

// Map common English keywords to Chinese for display
const KEYWORD_LABELS_ZH = {
  superconductivity: "超导", superconducting: "超导", superconductor: "超导体",
  magnetic: "磁性", magnetism: "磁性", ferromagnetic: "铁磁", antiferromagnetic: "反铁磁",
  quantum: "量子", topological: "拓扑", topology: "拓扑",
  spin: "自旋", spins: "自旋", electron: "电子", electrons: "电子",
  phonon: "声子", phonons: "声子", exciton: "激子", polariton: "极化激元",
  graphene: "石墨烯", "transition metal dichalcogenide": "TMDC", mos2: "MoS₂",
  transport: "输运", thermal: "热学", optical: "光学",
  disorder: "无序", localization: "局域化",
  phase: "相变", transition: "相变", critical: "临界",
  bose: "玻色", fermi: "费米", fermions: "费米子", bosons: "玻色子",
  dft: "DFT", "density functional theory": "DFT",
  "machine learning": "机器学习", "neural network": "神经网络",
  "monte carlo": "蒙特卡洛", dmrg: "DMRG", "tensor network": "张量网络",
  hubbard: "Hubbard", heisenberg: "Heisenberg", ising: "Ising",
  moire: "莫尔条纹", "2d materials": "二维材料", "van der waals": "范德华",
  perovskite: "钙钛矿", nanowire: "纳米线", film: "薄膜", crystal: "晶体",
  insulator: "绝缘体", "mott insulator": "Mott绝缘体",
  semimetal: "半金属", weyl: "Weyl", dirac: "Dirac",
  hall: "霍尔", nematic: "向列相", "charge density wave": "电荷密度波",
  "spin liquid": "自旋液体", skyrmion: "斯格明子",
};

function labelize(tag) {
  const lower = String(tag).toLowerCase();
  if (CATEGORY_LABELS_ZH[tag]) return CATEGORY_LABELS_ZH[tag];
  if (KEYWORD_LABELS_ZH[tag]) return KEYWORD_LABELS_ZH[tag];
  if (KEYWORD_LABELS_ZH[lower]) return KEYWORD_LABELS_ZH[lower];
  // Try partial match
  for (const [en, zh] of Object.entries(KEYWORD_LABELS_ZH)) {
    if (lower.includes(en) || en.includes(lower)) return zh;
  }
  // Clean up common arXiv prefixes
  return tag.replace(/^cond-mat\./, "").replace(/^physics\./, "").replace(/-/g, " ");
}

function formatDate(value) {
  if (!value) return "";
  const date = new Date(value);
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric", month: "2-digit", day: "2-digit",
  }).format(date);
}

function formatUpdateTime(value) {
  if (!value || !Number.isFinite(new Date(value).getTime())) return "未知";
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hour12: false,
  }).format(new Date(value));
}

function renderDataStatus() {
  const messages = [];
  const prb = state.data.source_status?.prb;
  if (state.source !== "arxiv" && prb?.status === "stale") {
    messages.push("PRB 暂未更新，保留 " + formatUpdateTime(prb.updated_at) + " 的结果。");
  } else if (state.source !== "arxiv" && prb?.status === "unavailable") {
    messages.push("PRB 暂时无法获取，arXiv 已更新。");
  }
  const papers = state.data.papers || [];
  if (!papers.some(paper => sourceOf(paper) === "arxiv")) {
    messages.push("当前数据缺少 arXiv 文章。");
  }
  const updated = new Date(state.data.generated_at).getTime();
  if (Number.isFinite(updated) && Date.now() - updated > 48 * 60 * 60 * 1000) {
    messages.push("最新数据停留在 " + formatUpdateTime(state.data.generated_at) + "。");
  }
  els.status.hidden = messages.length === 0;
  els.status.textContent = messages.join(" ");
}

function escapeHtml(value) {
  return String(value || "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function uniqCategories(papers) {
  const seen = new Set();
  const result = [];
  for (const paper of papers) {
    for (const category of paper.categories || []) {
      if ((category === "prb" || category.startsWith("cond-mat.")) && !seen.has(category)) {
        seen.add(category);
        result.push(category);
      }
    }
  }
  return result;
}

function sourceOf(paper) {
  return paper.source || "arxiv";
}

function sourceFilteredPapers() {
  const papers = state.data?.papers || [];
  if (state.source === "all") return papers;
  return papers.filter((paper) => sourceOf(paper) === state.source);
}

function filteredPapers() {
  const papers = sourceFilteredPapers();
  return papers.filter((paper) => {
    const categoryOk = state.category === "all" || (paper.categories || []).includes(state.category);
    if (!state.query) return categoryOk;
    const haystack = [
      paper.title,
      paper.abstract,
      paper.study_overview_zh,
      paper.abstract_summary_zh,
      paper.main_content_zh,
      paper.method_zh,
      paper.novelty_zh,
      paper.limitations_zh,
      (paper.authors || []).join(" "),
      (paper.keywords || []).join(" "),
    ].join(" ").toLowerCase();
    const q = state.query.toLowerCase();
    // Support multi-word search
    const terms = q.split(/\s+/).filter(Boolean);
    const queryOk = terms.every(function(t) { return haystack.includes(t); });
    return categoryOk && queryOk;
  });
}

function renderMetrics(papers) {
  const llmCount = papers.filter(function(p) {
    return (p.summary_mode || "").startsWith("llm-");
  }).length;
  const prbCount = papers.filter(function(p) { return p.source === "prb"; }).length;
  els.summary.innerHTML =
    '<div class="metric"><div class="label">凝聚态论文</div><div class="value">' + papers.length + '</div></div>' +
    '<div class="metric"><div class="label">AI 总结</div><div class="value">' + llmCount + '</div></div>' +
    '<div class="metric"><div class="label">PRB 论文</div><div class="value">' + prbCount + '</div></div>' +
    '<div class="metric"><div class="label">更新于（北京时间）</div><div class="value metric-time">' + formatUpdateTime(state.data.generated_at) + '</div></div>';
}

function renderSourceFilters(papers) {
  const counts = {
    all: papers.length,
    arxiv: papers.filter(function(p) { return sourceOf(p) === "arxiv"; }).length,
    prb: papers.filter(function(p) { return sourceOf(p) === "prb"; }).length,
  };
  const options = [
    ["all", "全部", counts.all],
    ["arxiv", "arXiv", counts.arxiv],
  ];
  if ((state.data.sources || []).includes("prb") || counts.prb > 0) {
    options.push(["prb", "PRB", counts.prb]);
  }
  var html = "";
  for (var i = 0; i < options.length; i++) {
    var item = options[i];
    var active = state.source === item[0] ? " active" : "";
    html += '<button class="source-chip' + active + '" aria-pressed="' + (state.source === item[0]) + '" data-source="' + item[0] + '">' +
      '<span>' + item[1] + '</span>' +
      '<strong>' + item[2] + '</strong>' +
    '</button>';
  }
  els.sourceFilters.innerHTML = html;
}

function renderFilters(papers) {
  const categories = uniqCategories(papers);
  var html = '<button class="chip' + (state.category === "all" ? " active" : "") + '" data-category="all">全部</button>';
  for (var i = 0; i < categories.length; i++) {
    var cat = categories[i];
    var active = state.category === cat ? " active" : "";
    html += '<button class="chip' + active + '" data-category="' + escapeHtml(cat) + '" title="' + escapeHtml(cat) + '">' + escapeHtml(labelize(cat)) + '</button>';
  }
  els.filters.innerHTML = html;
}

function renderSummaryField(label, value, highlight) {
  if (!value) return "";
  var cls = highlight ? "summary-block highlight" : "summary-block";
  return '<div class="' + cls + '"><div class="summary-label">' + label + '</div><div class="summary-text">' + escapeHtml(value) + '</div></div>';
}

function renderPapers(papers) {
  if (!papers.length) {
    var message = "没有匹配的论文。";
    if (!sourceFilteredPapers().length) {
      message = state.source === "prb" ? "当前没有可用的 PRB 文章。" : "当前没有可用的论文数据。";
    }
    els.list.innerHTML = '<div class="paper"><div class="muted">' + message + '</div></div>';
    return;
  }

  // Group papers by publishing date section
  var sections = state.data.listing_sections || [];
  var sectionMap = {};
  if (sections.length) {
    for (var si = 0; si < sections.length; si++) {
      var ids = sections[si].ids || [];
      for (var ii = 0; ii < ids.length; ii++) {
        sectionMap[String(ids[ii]).replace(/v\d+$/, "")] = si;
      }
    }
  }

  var currentSection = -1;
  var html = "";

  for (var i = 0; i < papers.length; i++) {
    var paper = papers[i];
    var sectionIdx = sectionMap[String(paper.arxiv_id).replace(/v\d+$/, "")];
    if (sectionIdx !== undefined && sectionIdx !== currentSection) {
      currentSection = sectionIdx;
      var sectionTitle = sections[currentSection].title || "";
      // Collapse repeated "showing X of X entries" for cleaner display
      sectionTitle = sectionTitle.replace(/\s*\(showing \d+ of \d+ entries\s*\)/i, "");
      html += '<div class="section-header">' + escapeHtml(sectionTitle) + '</div>';
    }

    var authors = escapeHtml((paper.authors || []).slice(0, 4).join(", "));
    var moreAuthors = (paper.authors || []).length > 4 ? " 等" : "";
    var title = escapeHtml(paper.title);
    var paperId = escapeHtml(paper.source === "prb" ? (paper.doi || paper.arxiv_id) : paper.arxiv_id);
    var absUrl = encodeURI(paper.abs_url || "#");
    var pdfUrl = encodeURI(paper.pdf_url || "#");
    var mode = paper.summary_mode || "";
    var isLLM = mode.startsWith("llm-");
    var modeLabel = isLLM ? "AI 导读" : "规则摘录";
    var modeClass = isLLM ? "badge-llm" : "badge-rule";

    // Tags from category and keywords
    var tagsHtml = "";
    if (paper.primary_category) {
      tagsHtml += '<span class="tag tag-cat">' + escapeHtml(labelize(paper.primary_category)) + '</span>';
    }
    var kw = (paper.keywords || []).slice(0, 5);
    for (var k = 0; k < kw.length; k++) {
      tagsHtml += '<span class="tag">' + escapeHtml(labelize(kw[k])) + '</span>';
    }

    html += '<article class="paper">' +
      '<div class="paper-head">' +
        '<div>' +
          '<h2 class="paper-title"><a href="' + absUrl + '" target="_blank" rel="noreferrer">' + title + '</a></h2>' +
          '<div class="paper-meta">' +
            '<span>' + authors + moreAuthors + '</span>' +
            '<span>·</span>' +
            '<span>' + formatDate(paper.published) + '</span>' +
            '<span>·</span>' +
            (paper.journal_ref ? '<span>' + escapeHtml(paper.journal_ref) + '</span><span>·</span>' : '') +
            (paper.source === "prb" ? '<span class="badge-excerpt">RSS 摘要片段</span><span>·</span>' : '') +
            '<span class="' + modeClass + '">' + modeLabel + '</span>' +
          '</div>' +
        '</div>' +
        '<div class="paper-id">' + paperId + '</div>' +
      '</div>' +
      '<div class="muted author-note">通讯作者：' + escapeHtml(paper.corresponding_author || "corresponding author not confirmed") + '</div>' +
      '<div class="paper-summary">' +
        renderSummaryField("研究对象与类型", paper.study_overview_zh) +
        renderSummaryField(isLLM ? "核心结论" : "结论原文", paper.abstract_summary_zh, isLLM) +
        renderSummaryField(isLLM ? "研究问题与证据" : "研究背景", paper.main_content_zh) +
        renderSummaryField("研究方法", paper.method_zh) +
      '</div>' +
      '<details class="paper-details"><summary>创新性、边界与原始摘要</summary>' +
        renderSummaryField("作者声称的新意", paper.novelty_zh) +
        renderSummaryField("适用边界与未确认信息", paper.limitations_zh || "仅依据摘要，未读取全文。") +
        renderSummaryField(paper.source === "prb" ? "原始 RSS 片段" : "原始摘要", paper.abstract) +
      '</details>' +
      '<div class="tags">' + tagsHtml + '</div>' +
      '<div class="paper-actions">' +
        '<a href="' + absUrl + '" target="_blank" rel="noreferrer">' + (paper.source === "prb" ? "PRB Article" : "arXiv Abstract") + '</a>' +
        (paper.source === "prb" ? (paper.doi ? '<a href="https://doi.org/' + escapeHtml(paper.doi) + '" target="_blank" rel="noreferrer">DOI</a>' : '') : '<a href="' + pdfUrl + '" target="_blank" rel="noreferrer">PDF</a>') +
      '</div>' +
    '</article>';
  }

  els.list.innerHTML = html;
}

function typesetMath() {
  if (window.MathJax && typeof window.MathJax.typesetPromise === "function") {
    window.MathJax.typesetPromise([els.list]).catch(function() {});
  }
}

function rerender() {
  var papers = filteredPapers();
  var visibleSourcePapers = sourceFilteredPapers();
  renderSourceFilters(state.data.papers || []);
  renderDataStatus();
  renderMetrics(papers);
  renderFilters(visibleSourcePapers);
  renderPapers(papers);
  els.meta.textContent = "显示 " + papers.length + " / " + visibleSourcePapers.length + " 篇论文";
  typesetMath();
}

async function loadData() {
  var response = await fetch("./data/latest.json", { cache: "no-store" });
  if (!response.ok) throw new Error("加载失败: " + response.status);
  var data = await response.json();
  if (!data || !Array.isArray(data.papers)) throw new Error("论文数据格式异常");
  state.data = data;
  els.title.textContent = state.data.site_title || "凝聚态论文日报";
  document.title = state.data.site_title || "凝聚态论文日报";
  if (state.source === "prb" && !(data.sources || []).includes("prb") && !data.papers.some(p => sourceOf(p) === "prb")) {
    state.source = "arxiv";
    state.category = "all";
  }
  rerender();
}

function showLoadError(error) {
  els.status.hidden = false;
  els.status.textContent = state.data ? "刷新失败，保留当前结果。" : "暂时无法加载论文数据，请稍后刷新。";
  if (!state.data) {
    els.meta.textContent = "加载失败：" + error.message;
    els.list.innerHTML = '<div class="paper"><div class="muted">论文数据暂不可用。</div></div>';
  }
}

els.search.addEventListener("input", function(event) {
  state.query = event.target.value.trim();
  rerender();
});

els.refresh.addEventListener("click", async function() {
  els.refresh.disabled = true;
  try { await loadData(); }
  catch (error) { showLoadError(error); }
  finally { els.refresh.disabled = false; }
});

els.sourceFilters.addEventListener("click", function(event) {
  var button = event.target.closest("[data-source]");
  if (!button) return;
  state.source = button.dataset.source;
  state.category = "all";
  rerender();
});

els.filters.addEventListener("click", function(event) {
  var button = event.target.closest("[data-category]");
  if (!button) return;
  state.category = button.dataset.category;
  rerender();
});

loadData().catch(showLoadError);
