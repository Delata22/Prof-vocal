/* Scène : moteur d'animations façon 3Blue1Brown pour les cartes du tableau.
 *
 * Le prof décrit des objets (réseau, plan, matrice, courbe…) et des animations, avec une API calquée sur
 * manim ; la mise en page (next_to, arrange…) et les flèches entre objets sont calculées ici.
 * Coordonnées en unités, origine au centre, y vers le haut ; cadre de 16 × 9 unités par défaut.
 * Une carte = scene({h: 7}, s => { …objets…; s.step("légende", () => { s.play(…) }); … }).
 * Voir API.md. Inclus en entier dans chaque carte par scene.py (les cartes sont des iframes sandbox).
 */
(function () {
"use strict";
const U = 60;                                   // px logiques par unité
const NS = "http://www.w3.org/2000/svg";
let S = null;                                   // scène en cours de construction / d'affichage

// ---------- couleurs (variante sombre, variante claire) ----------
const PAL = {
  BLUE: ["#58C4DD", "#1C6FA8"], TEAL: ["#5CD0B3", "#0E8673"], GREEN: ["#83C167", "#3B8A27"],
  YELLOW: ["#F4D345", "#A87C00"], GOLD: ["#F0AC5F", "#B5620B"], RED: ["#FC6255", "#CF2B1F"],
  MAROON: ["#C55F73", "#9A2C47"], PURPLE: ["#B189C6", "#7A4C9E"], PINK: ["#E36FCE", "#B0329C"],
  ORANGE: ["#FF8C3A", "#D45A00"], GREY: ["#8E8D88", "#8A8882"],
};
const isDark = () => matchMedia("(prefers-color-scheme: dark)").matches;
function cssVar(n, d) { const v = getComputedStyle(document.documentElement).getPropertyValue(n).trim(); return v || d; }
function col(c) {
  if (c == null || c === "none") return null;
  const k = String(c).toUpperCase();
  if (PAL[k]) return PAL[k][isDark() ? 0 : 1];
  if (k === "FG") return cssVar("--fg", isDark() ? "#ecebe6" : "#1f1e1c");
  if (k === "MUTED") return cssVar("--muted", isDark() ? "#a3a19a" : "#6d6b65");
  if (k === "BG") return cssVar("--bg", isDark() ? "#262624" : "#ffffff");
  return c;
}
function rgb(h) {
  h = col(h) || "#000";
  if (h[0] !== "#") return [128, 128, 128];
  if (h.length === 4) h = "#" + h[1] + h[1] + h[2] + h[2] + h[3] + h[3];
  return [1, 3, 5].map(i => parseInt(h.slice(i, i + 2), 16));
}
const hex = c => "#" + c.map(v => Math.round(Math.max(0, Math.min(255, v))).toString(16).padStart(2, "0")).join("");
const isColor = v => typeof v === "string" && (v[0] === "#" || PAL[v.toUpperCase()] || ["FG", "MUTED", "BG"].includes(v.toUpperCase()));

// ---------- vecteurs ----------
const add = (a, b) => [a[0] + b[0], a[1] + b[1]], sub = (a, b) => [a[0] - b[0], a[1] - b[1]];
const mul = (a, k) => [a[0] * k, a[1] * k], len = a => Math.hypot(a[0], a[1]);
const unit = a => { const l = len(a) || 1; return [a[0] / l, a[1] / l]; };
const lerp = (a, b, t) => a + (b - a) * t;
const lerp2 = (a, b, t) => [lerp(a[0], b[0], t), lerp(a[1], b[1], t)];
const px = p => [p[0] * U, -p[1] * U];
const f2 = v => Math.round(v * 100) / 100;
const UP = [0, 1], DOWN = [0, -1], LEFT = [-1, 0], RIGHT = [1, 0], ORIGIN = [0, 0];
const UL = [-1, 1], UR = [1, 1], DL = [-1, -1], DR = [1, -1];

function svg(tag, attrs) { const e = document.createElementNS(NS, tag); for (const k in attrs || {}) e.setAttribute(k, attrs[k]); return e; }
const isMob = m => m instanceof Mob;
function P(v) { return typeof v === "function" ? v() : isMob(v) ? v.center() : v; }   // point d'un objet / d'une fonction
function boxOf(t) { if (isMob(t)) return t.bbox(); const p = P(t); return {x0: p[0], y0: p[1], x1: p[0], y1: p[1]}; }
function edgePoint(b, d) {
  const cx = (b.x0 + b.x1) / 2, cy = (b.y0 + b.y1) / 2;
  return [cx + Math.sign(d[0]) * (b.x1 - b.x0) / 2, cy + Math.sign(d[1]) * (b.y1 - b.y0) / 2];
}
// point du bord d'un objet dans la direction d'un point (cercle exact, sinon boîte)
function boundary(m, toward) {
  const c = m.center(), d = unit(sub(toward, c));
  if (m.r !== undefined) return add(c, mul(d, m.r * (m.circle ? m.circle.s : m.s)));
  const b = m.bbox(), hw = (b.x1 - b.x0) / 2, hh = (b.y1 - b.y0) / 2;
  const t = Math.min(Math.abs(d[0]) > 1e-9 ? hw / Math.abs(d[0]) : 1e9, Math.abs(d[1]) > 1e-9 ? hh / Math.abs(d[1]) : 1e9);
  return add(c, mul(d, t));
}

// ---------- objets ----------
class Mob {
  constructor() { this.x = 0; this.y = 0; this.s = 1; this.op = 1; this.color = "FG"; this.el = svg("g"); this.ups = []; this._args = []; }
  lb() { return {x0: 0, y0: 0, x1: 0, y1: 0}; }
  bbox() { const b = this.lb(); return {x0: this.x + b.x0 * this.s, y0: this.y + b.y0 * this.s, x1: this.x + b.x1 * this.s, y1: this.y + b.y1 * this.s}; }
  center() { const b = this.bbox(); return [(b.x0 + b.x1) / 2, (b.y0 + b.y1) / 2]; }
  get width() { const b = this.bbox(); return b.x1 - b.x0; }
  get height() { const b = this.bbox(); return b.y1 - b.y0; }
  edge(d) { return edgePoint(this.bbox(), d); }
  get_top() { return this.edge(UP); } get_bottom() { return this.edge(DOWN); }
  get_left() { return this.edge(LEFT); } get_right() { return this.edge(RIGHT); }
  state() { return Object.assign({x: this.x, y: this.y, s: this.s, op: this.op, color: this.color}, this.extra()); }
  extra() { return {}; }
  setState(st) { for (const k in st) if (st[k] !== undefined) this[k] = st[k]; return this; }
  leaves() { return [this]; }
  render() {
    this.el.setAttribute("transform", `translate(${f2(this.x * U)} ${f2(-this.y * U)}) scale(${this.s})`);
    this.el.style.opacity = Math.max(0, Math.min(1, this.op));
    this.draw();
  }
  draw() {}
  // --- placement (modifient l'objet, renvoient l'objet : chaînables, et utilisables dans .animate) ---
  shift(v) { this.x += v[0]; this.y += v[1]; return this; }
  move_to(p) { return this.shift(sub(P(p), this.center())); }
  next_to(t, d = RIGHT, buff = 0.25) {
    const te = edgePoint(boxOf(t), d), me = edgePoint(this.bbox(), mul(d, -1));
    return this.shift(add(sub(te, me), [Math.sign(d[0]) * buff, Math.sign(d[1]) * buff]));
  }
  align_to(t, d) {
    const te = edgePoint(boxOf(t), d), me = edgePoint(this.bbox(), d);
    return this.shift([d[0] ? te[0] - me[0] : 0, d[1] ? te[1] - me[1] : 0]);
  }
  to_edge(d, buff = 0.4) {
    const F = S.frame(), me = edgePoint(this.bbox(), d);
    const fe = [d[0] > 0 ? F.x1 - buff : F.x0 + buff, d[1] > 0 ? F.y1 - buff : F.y0 + buff];
    return this.shift([d[0] ? fe[0] - me[0] : 0, d[1] ? fe[1] - me[1] : 0]);
  }
  to_corner(d = UL, buff = 0.4) { return this.to_edge([d[0], 0], buff).to_edge([0, d[1]], buff); }
  scale(k) { const c = this.center(); this.s *= k; return this.move_to(c); }
  set_color(c) { this.color = c; return this; }
  set_opacity(o) { this.op = o; return this; }
  set(props) { Object.assign(this, props); if (this.remeasure) this.remeasure(); return this; }
  copy() { const m = new this.constructor(...this._args); m.setState(this.state()); return m; }
  add_updater(f) { this.ups.push(f); return this; }
  get animate() { return animBuilder(this); }
  get node() { return this.el; }
}

class Group extends Mob {
  constructor(...ms) { super(); this.kids = ms.flat(); }
  bbox() {
    const bs = this.kids.map(k => k.bbox());
    if (!bs.length) return {x0: this.x, y0: this.y, x1: this.x, y1: this.y};
    return {x0: Math.min(...bs.map(b => b.x0)), y0: Math.min(...bs.map(b => b.y0)),
            x1: Math.max(...bs.map(b => b.x1)), y1: Math.max(...bs.map(b => b.y1))};
  }
  state() { return {kids: this.kids.map(k => k.state())}; }
  setState(st) { if (st.kids) st.kids.forEach((s, i) => this.kids[i] && this.kids[i].setState(s)); return this; }
  leaves() { return this.kids.flatMap(k => k.leaves()); }
  render() { this.kids.forEach(k => k.render()); }
  shift(v) { this.kids.forEach(k => k.shift(v)); return this; }
  scale(k) {
    const c = this.center();
    this.kids.forEach(m => { const mc = m.center(); m.scale(k); m.move_to(add(c, mul(sub(mc, c), k))); });
    return this;
  }
  set_color(c) { this.kids.forEach(k => k.set_color(c)); return this; }
  set_opacity(o) { this.kids.forEach(k => k.set_opacity(o)); return this; }
  arrange(d = RIGHT, buff = 0.3) {
    const c = this.center();
    for (let i = 1; i < this.kids.length; i++) this.kids[i].next_to(this.kids[i - 1], d, buff);
    return this.move_to(c);
  }
  arrange_grid(cols = 2, buff = 0.4) {
    const c = this.center(), w = Math.max(...this.kids.map(k => k.width)), h = Math.max(...this.kids.map(k => k.height));
    this.kids.forEach((k, i) => k.move_to([(i % cols) * (w + buff), -Math.floor(i / cols) * (h + buff)]));
    return this.move_to(c);
  }
  copy() { return new Group(...this.kids.map(k => k.copy())); }
  get length() { return this.kids.length; }
  at(i) { return this.kids[i]; }
}

// --- formes : contour (color), remplissage (fill, fop), épaisseur (sw), tracé progressif (draw_t) ---
class Shape extends Mob {
  constructor(o = {}) {
    super();
    this.color = o.color === undefined ? "FG" : o.color; this.fill = o.fill === undefined ? null : o.fill;
    this.fop = o.fop === undefined ? (this.fill ? 0.35 : 0) : o.fop; this.sw = o.width || 3; this.draw_t = 1;
    this.dash = o.dash || null;
    this.p = svg("path", {pathLength: 1, "stroke-linejoin": "round", "stroke-linecap": "round"}); this.el.append(this.p);
  }
  extra() { return {fill: this.fill, fop: this.fop, sw: this.sw, draw_t: this.draw_t}; }
  set_color(c) { this.color = c; if (this.fill) this.fill = c; return this; }
  d() { return ""; }
  draw() {
    const p = this.p, c = col(this.color);
    p.setAttribute("d", this.d());
    p.setAttribute("stroke", c || "none"); p.setAttribute("stroke-width", this.sw);
    p.setAttribute("fill", col(this.fill) || "none");
    p.setAttribute("fill-opacity", this.fill ? this.fop * Math.min(1, Math.max(0, this.draw_t * 2 - 1)) : 0);
    if (this.dash && this.draw_t >= 1) { p.setAttribute("stroke-dasharray", this.dash); p.removeAttribute("pathLength"); }
    else if (this.draw_t < 1) { p.setAttribute("pathLength", 1); p.setAttribute("stroke-dasharray", `${Math.max(0, this.draw_t)} 1.01`); }
    else { p.setAttribute("pathLength", 1); p.removeAttribute("stroke-dasharray"); }
  }
}
class Circle extends Shape {
  constructor(o = {}) { super(o); this._args = [o]; this.r = o.r || 0.5; if (o.at) this.move_to(o.at); }
  extra() { return Object.assign(super.extra(), {r: this.r}); }
  lb() { return {x0: -this.r, y0: -this.r, x1: this.r, y1: this.r}; }
  d() { const r = this.r * U; return `M ${r} 0 A ${r} ${r} 0 1 0 ${-r} 0 A ${r} ${r} 0 1 0 ${r} 0`; }
}
class Dot extends Circle {
  constructor(at = ORIGIN, o = {}) {
    const c = o.color || "FG";
    super(Object.assign({r: 0.08, fill: c, fop: 1, color: null}, o, {at: undefined}));
    this._args = [at, o]; this.dotColor = c; this.move_to(at);
  }
  set_color(c) { this.fill = c; return this; }
}
class Rect extends Shape {
  constructor(o = {}) { super(o); this._args = [o]; this.w = o.w || 2; this.h = o.h || 1; this.round = o.round ?? 0.12; if (o.at) this.move_to(o.at); }
  extra() { return Object.assign(super.extra(), {w: this.w, h: this.h}); }
  lb() { return {x0: -this.w / 2, y0: -this.h / 2, x1: this.w / 2, y1: this.h / 2}; }
  d() {
    const w = this.w * U / 2, h = this.h * U / 2, r = Math.min(this.round * U, w, h);
    return `M ${-w + r} ${-h} H ${w - r} A ${r} ${r} 0 0 1 ${w} ${-h + r} V ${h - r} A ${r} ${r} 0 0 1 ${w - r} ${h} ` +
           `H ${-w + r} A ${r} ${r} 0 0 1 ${-w} ${h - r} V ${-h + r} A ${r} ${r} 0 0 1 ${-w + r} ${-h} Z`;
  }
}
class Poly extends Shape {   // points en coordonnées du monde (ligne brisée ; closed : polygone)
  constructor(pts, o = {}) { super(o); this._args = [pts, o]; this.pts = pts.map(p => p.slice()); this.closed = !!o.closed; }
  extra() { return Object.assign(super.extra(), {pts: this.pts.map(p => p.slice())}); }
  lb() {
    const xs = this.pts.map(p => p[0]), ys = this.pts.map(p => p[1]);
    return {x0: Math.min(...xs), y0: Math.min(...ys), x1: Math.max(...xs), y1: Math.max(...ys)};
  }
  d() { return this.pts.map((p, i) => (i ? "L " : "M ") + px(p).map(f2).join(" ")).join(" ") + (this.closed ? " Z" : ""); }
  shift(v) { this.pts = this.pts.map(p => add(p, v)); return this; }
  scale(k) { const c = this.center(); this.pts = this.pts.map(p => add(c, mul(sub(p, c), k))); return this; }
  segments() { const s = []; for (let i = 1; i < this.pts.length; i++) s.push([this.pts[i - 1], this.pts[i]]); return s; }
}

// --- segments et flèches : extrémités = points, objets (bord le plus proche) ou fonctions ---
class Line extends Mob {
  constructor(A, B, o = {}) {
    super(); this._args = [A, B, o];
    this.A = A; this.B = B; this.color = o.color || "FG"; this.sw = o.width || 3; this.draw_t = 1;
    this.dash = o.dash || null; this.tip = !!o.tip; this.tip2 = !!o.tip2;
    this.buff = o.buff ?? ((isMob(A) || isMob(B)) ? 0.08 : 0); this.bg = !!o.bg;
    this.p = svg("path", {"stroke-linecap": "round", fill: "none"}); this.el.append(this.p);
    this.h1 = svg("path"); this.h2 = svg("path"); this.el.append(this.h1, this.h2);
    this.update_ends();
  }
  extra() { return {A: Array.isArray(this.A) ? this.A.slice() : undefined, B: Array.isArray(this.B) ? this.B.slice() : undefined, sw: this.sw, draw_t: this.draw_t}; }
  update_ends() {
    const ca = P(this.A), cb = P(this.B);
    let a = isMob(this.A) ? boundary(this.A, cb) : ca, b = isMob(this.B) ? boundary(this.B, ca) : cb;
    const d = unit(sub(b, a));
    if (isMob(this.A)) a = add(a, mul(d, this.buff));
    if (isMob(this.B)) b = sub(b, mul(d, this.buff));
    this.a = a; this.b = b;
  }
  lb() { this.update_ends(); return {x0: Math.min(this.a[0], this.b[0]), y0: Math.min(this.a[1], this.b[1]), x1: Math.max(this.a[0], this.b[0]), y1: Math.max(this.a[1], this.b[1])}; }
  bbox() { return this.lb(); }
  start() { this.update_ends(); return this.a; } end() { this.update_ends(); return this.b; }
  point_at(t) { this.update_ends(); return lerp2(this.a, this.b, t); }
  direction() { this.update_ends(); return unit(sub(this.b, this.a)); }
  shift(v) { if (Array.isArray(this.A)) this.A = add(this.A, v); if (Array.isArray(this.B)) this.B = add(this.B, v); return this; }
  scale(k) {
    const c = this.center();
    if (Array.isArray(this.A)) this.A = add(c, mul(sub(this.A, c), k));
    if (Array.isArray(this.B)) this.B = add(c, mul(sub(this.B, c), k));
    return this;
  }
  put_start_and_end_on(a, b) { this.A = a; this.B = b; return this; }
  segments() { this.update_ends(); return [[this.a, this.b]]; }
  render() { this.el.style.opacity = Math.max(0, Math.min(1, this.op)); this.draw(); }
  draw() {
    this.update_ends();
    const c = col(this.color), tl = Math.min(0.28 * this.sw / 4 + 0.1, len(sub(this.b, this.a)) * 0.4), d = unit(sub(this.b, this.a));
    let a = this.a, b = this.b;
    if (this.tip) b = sub(b, mul(d, tl * 0.8));
    if (this.tip2) a = add(a, mul(d, tl * 0.8));
    const e = lerp2(a, b, Math.max(0, Math.min(1, this.draw_t)));
    this.p.setAttribute("d", `M ${px(a).map(f2).join(" ")} L ${px(e).map(f2).join(" ")}`);
    this.p.setAttribute("stroke", c); this.p.setAttribute("stroke-width", this.sw);
    if (this.dash) this.p.setAttribute("stroke-dasharray", this.dash); else this.p.removeAttribute("stroke-dasharray");
    const head = (el, at, dir, on) => {
      if (!on || this.draw_t <= 0.02) { el.setAttribute("d", ""); return; }
      const n = [-dir[1], dir[0]], w = tl * 0.45, base = sub(at, mul(dir, tl * 0.2));
      const pts = [add(base, mul(dir, tl)), add(base, mul(n, w)), sub(base, mul(n, w))].map(p => px(p).map(f2).join(" "));
      el.setAttribute("d", `M ${pts[0]} L ${pts[1]} L ${pts[2]} Z`); el.setAttribute("fill", c);
    };
    head(this.h1, e, d, this.tip); head(this.h2, a, mul(d, -1), this.tip2 && this.draw_t >= 1);
  }
  // étiquette posée le long du segment (t = 0 au départ, 1 à l'arrivée), du côté side (+1 : à gauche du sens)
  label(tex, o = {}) {
    const side = o.side ?? 1, lab = isMob(tex) ? tex : Tex(tex, Object.assign({pill: side === 0}, o)), t = o.t ?? 0.5, buff = o.buff ?? 0.12;
    const place = () => {
      const p = this.point_at(t), d = this.direction(), n = mul([-d[1], d[0]], side);
      const off = side === 0 ? 0 : buff + Math.abs(n[0]) * lab.width / 2 + Math.abs(n[1]) * lab.height / 2;
      lab.move_to(add(p, mul(n, off)));
    };
    place(); lab.add_updater(place); lab.owner = this;
    return lab;
  }
}
const Arrow = (A, B, o = {}) => new Line(A, B, Object.assign({tip: true, width: 4}, o));
const DoubleArrow = (A, B, o = {}) => new Line(A, B, Object.assign({tip: true, tip2: true, width: 4}, o));
const DashedLine = (A, B, o = {}) => new Line(A, B, Object.assign({dash: "8 7"}, o));

// --- texte et formules (KaTeX dans un foreignObject) ---
const meas = () => document.getElementById("sc-meas");
// \blue{x}, \red{x}… : couleurs de la palette dans les formules (même couleur que l'objet dessiné)
const COLOR_CMD = new RegExp("\\\\(" + Object.keys(PAL).map(k => k.toLowerCase()).join("|") + ")(?![a-zA-Z])", "g");
function renderMath(t) {
  t = t.replace(COLOR_CMD, (_, k) => `\\textcolor{${col(k.toUpperCase())}}`);
  try { return katex.renderToString(t, {throwOnError: false, displayMode: false, strict: false}); }
  catch (e) { return `<span style="color:red">${t}</span>`; }
}
const esc = s => s.replace(/[&<>]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;"})[c]);
class TexMob extends Mob {
  constructor(tex, o = {}) {
    super(); this._args = [tex, o];
    this.tex = String(tex); this.text = !!o.text; this.size = o.size || 1; this.color = o.color || "FG";
    this.halo = o.halo !== false; this.pill = !!o.pill; this.wrap = o.width || null; this.draw_t = 1; this.bold = !!o.bold;
    this.div = document.createElement("div"); this.div.className = "sc-tex"; this.html_ = true;
    this.remeasure();
    if (o.at) this.move_to(o.at);
  }
  extra() { return {tex: this.tex, draw_t: this.draw_t}; }
  html() {
    if (!this.text) return renderMath(this.tex.startsWith("\\displaystyle") ? this.tex : "\\displaystyle " + this.tex);
    return this.tex.split(/(\$[^$]+\$)/).map(p => p.startsWith("$") && p.endsWith("$") && p.length > 2 ? renderMath(p.slice(1, -1)) : esc(p)).join("");
  }
  style(el) {
    el.style.fontSize = (this.text ? 27 : 25) * this.size + "px";
    el.style.fontWeight = this.bold ? "bold" : "normal";
    el.style.whiteSpace = this.wrap ? "normal" : "nowrap";
    el.style.width = this.wrap ? this.wrap * U + "px" : "auto";
    el.style.textAlign = this.wrap ? "center" : "left";
  }
  remeasure() {
    const m = meas(); m.innerHTML = ""; const d = document.createElement("div"); d.className = "sc-tex"; this.style(d);
    d.style.display = "inline-block"; d.innerHTML = this._html = this.html(); m.append(d);
    const r = d.getBoundingClientRect(); this.w = r.width / U; this.h = r.height / U; this._shown = null;
  }
  setState(st) { const t = this.tex; super.setState(st); if (this.tex !== t) this.remeasure(); return this; }
  lb() { return {x0: -this.w / 2, y0: -this.h / 2, x1: this.w / 2, y1: this.h / 2}; }
  get node() { return this.div; }
  render() {         // position à l'écran : même transformation que le SVG (S.k, S.ox, S.oy, calculés à chaque pas)
    const X = (this.x * U + S.W * U / 2) * S.k, Y = (-this.y * U + S.H * U / 2) * S.k;
    this.div.style.left = f2(X) + "px"; this.div.style.top = f2(Y) + "px";
    this.div.style.transform = `translate(-50%, -50%) scale(${f2(S.k * this.s * 1000) / 1000})`;
    this.div.style.opacity = Math.max(0, Math.min(1, this.op));
    this.draw();
  }
  draw() {
    const pad = this.pill ? 0 : 6;
    const key = this._html + "|" + this.size;
    if (this._shown !== key) { this.style(this.div); this.div.innerHTML = this._html; this._shown = key; }
    this.div.style.padding = pad + "px"; this.div.style.color = col(this.color);
    const bg = col("BG");
    this.div.style.textShadow = this.halo && !this.pill ? `0 0 3px ${bg}, 0 0 3px ${bg}, 0 0 2px ${bg}, 0 0 6px ${bg}` : "none";
    if (this.pill) { this.div.style.background = bg; this.div.style.borderRadius = "8px"; this.div.style.padding = "0 5px";
      this.div.style.boxShadow = `0 0 0 1.5px ${col(this.pillColor || this.color)}`; }
    this.div.style.clipPath = this.draw_t < 1 ? `inset(0 ${f2((1 - Math.max(0, this.draw_t)) * 100)}% 0 0)` : "none";
  }
}
const Tex = (t, o) => new TexMob(t, o);
const Text = (t, o = {}) => new TexMob(t, Object.assign({text: true}, o));

class Num extends TexMob {        // nombre animable : s.play(n.animate.set({value: 5}))
  constructor(v, o = {}) { super(fmt(v, o.digits ?? 0), o); this._args = [v, o]; this.value = v; this.digits = o.digits ?? 0; }
  extra() { return Object.assign(super.extra(), {value: this.value}); }
  setState(st) { super.setState(st); this.syncValue(); return this; }
  set(p) { Object.assign(this, p); this.syncValue(); return this; }
  syncValue() { const t = fmt(this.value, this.digits); if (t !== this.tex) { const c = this.center(); this.tex = t; this.remeasure(); this.move_to(c); } }
}
function fmt(v, d) { const s = Number(v).toFixed(d); return s.replace("-", "-"); }

// --- groupes prêts à l'emploi ---
class Node extends Group {        // cercle + valeur dedans (+ nom au-dessus)
  constructor(tex, o = {}) {
    const r = o.r || 0.42, c = o.color || "BLUE";
    const circle = new Circle({r, color: c, fill: c, fop: o.fop ?? 0.22, width: 3});
    const label = Tex(tex, {size: o.size || 0.9, color: o.text || "FG", halo: false});
    const kids = [circle, label];
    let name = null;
    if (o.name) { name = Tex(o.name, {size: 0.65, color: "MUTED"}); name.next_to(circle, o.name_dir || UP, 0.12); kids.push(name); }
    super(...kids); this._args = [tex, o]; this.circle = circle; this.label = label; this.name = name; this.r = r;
    if (o.at) this.move_to(o.at);
  }
  center() { return this.circle.center(); }
  set_value(tex, o) { return ChangeTex(this.label, tex, o); }
}
class Box extends Group {         // case arrondie + contenu
  constructor(tex, o = {}) {
    const label = isMob(tex) ? tex : Tex(tex, {size: o.size || 0.9, color: o.text || "FG", halo: false});
    const c = o.color || "BLUE";
    const rect = new Rect({w: Math.max(o.w || 0, label.width + 0.4), h: Math.max(o.h || 0, label.height + 0.25), color: c, fill: o.fill === undefined ? c : o.fill, fop: o.fop ?? 0.18, width: 2.5});
    const kids = [rect, label];
    let name = null;
    if (o.name) { name = Tex(o.name, {size: 0.65, color: "MUTED"}); name.next_to(rect, o.name_dir || UP, 0.1); kids.push(name); }
    super(...kids); this._args = [tex, o]; this.rect = rect; this.label = label; this.name = name;
    if (o.at) this.move_to(o.at);
  }
  center() { return this.rect.center(); }
  bbox() { return this.rect.bbox(); }
  set_value(tex, o) { return ChangeTex(this.label, tex, o); }
}

class Matrix extends Group {
  constructor(rows, o = {}) {
    const ent = rows.map((r, i) => r.map((v, j) => isMob(v) ? v : Tex(String(v), {size: o.size || 1, color: (o.colors && o.colors[i] && o.colors[i][j]) || o.color || "FG"})));
    const m = ent.length, n = Math.max(...ent.map(r => r.length));
    const hb = o.hbuff ?? 0.5, vb = o.vbuff ?? 0.28;
    const cw = [...Array(n)].map((_, j) => Math.max(0.35, ...ent.map(r => r[j] ? r[j].width : 0)));
    const rh = ent.map(r => Math.max(0.4, ...r.map(e => e.height)));
    const W = cw.reduce((a, b) => a + b, 0) + hb * (n - 1), H = rh.reduce((a, b) => a + b, 0) + vb * (m - 1);
    let y = H / 2;
    ent.forEach((r, i) => {
      let x = -W / 2; y -= rh[i] / 2;
      r.forEach((e, j) => { e.move_to([x + cw[j] / 2, y]); x += cw[j] + hb; });
      y -= rh[i] / 2 + vb;
    });
    const pad = 0.16, bx = W / 2 + pad, by = H / 2 + 0.12, t = 0.14, bc = o.bracket || "FG";
    const lb = new Poly([[-bx + t, by], [-bx, by], [-bx, -by], [-bx + t, -by]], {color: bc, width: 2.5});
    const rb = new Poly([[bx - t, by], [bx, by], [bx, -by], [bx - t, -by]], {color: bc, width: 2.5});
    const labs = [];
    const rowY = ent.map(r => r[0].center()[1]), colX = [...Array(n)].map((_, j) => ent[0][j] ? ent[0][j].center()[0] : 0);
    if (o.rowLabels) o.rowLabels.forEach((t, i) => labs.push(Tex(t, {size: 0.65, color: (o.rowColors && o.rowColors[i]) || "MUTED"}).move_to([0, rowY[i]]).next_to([bx, rowY[i]], RIGHT, 0.2)));
    if (o.colLabels) o.colLabels.forEach((t, j) => labs.push(Tex(t, {size: 0.65, color: (o.colColors && o.colColors[j]) || "MUTED"}).move_to([colX[j], 0]).next_to([colX[j], by], UP, 0.12)));
    if (o.rowLabels) labs.slice(0, o.rowLabels.length).forEach((l, i) => l.move_to([l.center()[0], rowY[i]]));
    if (o.colLabels) labs.slice(o.rowLabels ? o.rowLabels.length : 0).forEach((l, j) => l.move_to([colX[j], l.center()[1]]));
    super(...ent.flat(), lb, rb, ...labs); this._args = [rows, o];
    this.ent = ent; this.brackets = new Group(lb, rb); this.labels = new Group(...labs);
    if (o.at) this.move_to(o.at);
  }
  entry(i, j) { return this.ent[i][j]; }
  row(i) { return new Group(...this.ent[i]); }
  col(j) { return new Group(...this.ent.map(r => r[j]).filter(Boolean)); }
  entries() { return new Group(...this.ent.flat()); }
}

class Table extends Group {       // grille de valeurs (pixels, noyau…) ; heat : couleur selon la valeur
  constructor(rows, o = {}) {
    const m = rows.length, n = Math.max(...rows.map(r => r.length));
    const tex = rows.map(r => r.map(v => isMob(v) ? v : Tex(String(v), {size: o.size || 0.8, halo: false})));
    const cw = o.cell || Math.max(0.7, ...tex.flat().map(t => t.width + 0.3)), ch = o.cellh || Math.max(0.6, ...tex.flat().map(t => t.height + 0.2));
    const nums = rows.flat().filter(v => typeof v === "number"), lo = Math.min(...nums), hi = Math.max(...nums);
    const cells = [], all = [];
    rows.forEach((r, i) => {
      cells.push([]);
      r.forEach((v, j) => {
        let fill = o.fill ? o.fill(v, i, j) : null, fop = 0.25;
        if (o.heat && typeof v === "number") { fill = o.heat === true ? "BLUE" : o.heat; fop = 0.08 + 0.6 * (hi > lo ? (v - lo) / (hi - lo) : 0.5); }
        const rect = new Rect({w: cw, h: ch, round: 0.03, color: o.line || "GREY", width: 1.5, fill, fop});
        const at = [(j - (n - 1) / 2) * cw, ((m - 1) / 2 - i) * ch];
        rect.move_to(at); tex[i][j].move_to(at);
        const cell = new Group(rect, tex[i][j]); cell.rect = rect; cell.label = tex[i][j];
        cells[i].push(cell); all.push(rect);
      });
    });
    const kids = [...all, ...tex.flat()];
    const labs = [];
    if (o.rowLabels) o.rowLabels.forEach((t, i) => { const l = Tex(t, {size: 0.7, color: "MUTED"}); l.next_to(cells[i][0].rect, LEFT, 0.15); labs.push(l); });
    if (o.colLabels) o.colLabels.forEach((t, j) => { const l = Tex(t, {size: 0.7, color: "MUTED"}); l.next_to(cells[0][j].rect, UP, 0.12); labs.push(l); });
    super(...kids, ...labs); this._args = [rows, o]; this.cells = cells;
    if (o.at) this.move_to(o.at);
  }
  cell(i, j) { return this.cells[i][j]; }
  row(i) { return new Group(...this.cells[i]); }
  col(j) { return new Group(...this.cells.map(r => r[j]).filter(Boolean)); }
}

class Brace extends Group {       // accolade le long d'un côté d'un objet, avec étiquette
  constructor(m, d = DOWN, o = {}) {
    const b = m.bbox(), buff = o.buff ?? 0.12, dep = 0.22;
    let pts;
    const curve = (a, z, s, n) => {      // a→z le long du côté, s : sens de la pointe, n : normale
      const L = sub(z, a), mid = lerp2(a, z, 0.5), q = Math.min(0.25, len(L) / 6);
      const u = unit(L), at = (p, k, h) => add(add(p, mul(u, k)), mul(n, h));
      return [a, at(a, q, dep * 0.5), at(mid, -q, dep * 0.5), at(mid, 0, dep), at(mid, q, dep * 0.5), at(z, -q, dep * 0.5), z];
    };
    if (d[1] < 0) pts = curve([b.x0, b.y0 - buff], [b.x1, b.y0 - buff], 0, DOWN);
    else if (d[1] > 0) pts = curve([b.x0, b.y1 + buff], [b.x1, b.y1 + buff], 0, UP);
    else if (d[0] < 0) pts = curve([b.x0 - buff, b.y1], [b.x0 - buff, b.y0], 0, LEFT);
    else pts = curve([b.x1 + buff, b.y1], [b.x1 + buff, b.y0], 0, RIGHT);
    const path = new Poly(pts, {color: o.color || "FG", width: 2.5});
    const kids = [path];
    let lab = null;
    if (o.label) { lab = isMob(o.label) ? o.label : Tex(o.label, {color: o.color || "FG", size: o.size || 0.85}); lab.next_to(path, d, 0.12); kids.push(lab); }
    super(...kids); this._args = [m, d, o]; this.path = path; this.lab = lab;
  }
}
const SurroundingRect = (m, o = {}) => {
  const b = m.bbox(), buff = o.buff ?? 0.15;
  return new Rect({w: b.x1 - b.x0 + 2 * buff, h: b.y1 - b.y0 + 2 * buff, at: [(b.x0 + b.x1) / 2, (b.y0 + b.y1) / 2], color: o.color || "YELLOW", width: o.width || 3, round: o.round ?? 0.1, fill: o.fill, fop: o.fop});
};

// --- repère avec graduations ; plot(f) trace une courbe ---
class Axes extends Group {
  constructor(o = {}) {
    const xr = o.x || [-1, 5, 1], yr = o.y || [-1, 3, 1], w = o.w || 8, h = o.h || 4.5;
    const at = o.at || ORIGIN, ox = at[0] - w / 2, oy = at[1] - h / 2;
    const c2p = (x, y) => [ox + (x - xr[0]) / (xr[1] - xr[0]) * w, oy + (y - yr[0]) / (yr[1] - yr[0]) * h];
    const x0 = (xr[0] <= 0 && 0 <= xr[1]) ? 0 : xr[0], y0 = (yr[0] <= 0 && 0 <= yr[1]) ? 0 : yr[0];
    const ac = o.color || "MUTED";
    const xa = new Line(c2p(xr[0], y0), add(c2p(xr[1], y0), [0.25, 0]), {tip: true, width: 2.5, color: ac});
    const ya = new Line(c2p(x0, yr[0]), add(c2p(x0, yr[1]), [0, 0.25]), {tip: true, width: 2.5, color: ac});
    const kids = [xa, ya], nums = [];
    const tick = (p, d) => new Line(sub(p, mul(d, 0.07)), add(p, mul(d, 0.07)), {width: 2, color: ac});
    const fmtN = v => Number.isInteger(v) ? String(v) : String(Math.round(v * 100) / 100);
    if (xr[2]) for (let x = Math.ceil(xr[0] / xr[2]) * xr[2]; x <= xr[1] + 1e-9; x += xr[2]) {
      if (Math.abs(x - x0) < 1e-9 && o.y !== false) continue;
      kids.push(tick(c2p(x, y0), UP));
      if (o.numbers !== false) { const t = Tex(fmtN(x), {size: 0.6, color: ac}); t.next_to(c2p(x, y0), DOWN, 0.15); nums.push(t); }
    }
    if (yr[2]) for (let y = Math.ceil(yr[0] / yr[2]) * yr[2]; y <= yr[1] + 1e-9; y += yr[2]) {
      if (Math.abs(y - y0) < 1e-9) continue;
      kids.push(tick(c2p(x0, y), RIGHT));
      if (o.numbers !== false) { const t = Tex(fmtN(y), {size: 0.6, color: ac}); t.next_to(c2p(x0, y), LEFT, 0.15); nums.push(t); }
    }
    if (o.xlabel) { const t = Tex(o.xlabel, {size: 0.8}); t.next_to(xa.end(), RIGHT, 0.15); nums.push(t); }
    if (o.ylabel) { const t = Tex(o.ylabel, {size: 0.8}); t.next_to(ya.end(), UP, 0.12); nums.push(t); }
    super(...kids, ...nums); this._args = [o];
    this.xr = xr; this.yr = yr; this.w = w; this.h = h; this.ox = ox; this.oy = oy; this.xaxis = xa; this.yaxis = ya;
  }
  c2p(x, y) { return [this.ox + (x - this.xr[0]) / (this.xr[1] - this.xr[0]) * this.w, this.oy + (y - this.yr[0]) / (this.yr[1] - this.yr[0]) * this.h]; }
  p2c(p) { return [this.xr[0] + (p[0] - this.ox) / this.w * (this.xr[1] - this.xr[0]), this.yr[0] + (p[1] - this.oy) / this.h * (this.yr[1] - this.yr[0])]; }
  shift(v) { super.shift(v); this.ox += v[0]; this.oy += v[1]; return this; }
  scale() { return this; }       // un repère ne se met pas à l'échelle : choisis w et h
  plot(f, o = {}) { return new Graph(this, f, o); }
  dot(x, y, o = {}) { return new Dot(this.c2p(x, y), o); }
  vline(x, y, o = {}) { return DashedLine(this.c2p(x, 0), this.c2p(x, y), Object.assign({width: 2, color: "MUTED"}, o)); }
  hline(x, y, o = {}) { return DashedLine(this.c2p(0, y), this.c2p(x, y), Object.assign({width: 2, color: "MUTED"}, o)); }
  area(f, a, b, o = {}) {
    const pts = [this.c2p(a, 0)];
    for (let i = 0; i <= 80; i++) { const x = a + (b - a) * i / 80; pts.push(this.c2p(x, f(x, o.k))); }
    pts.push(this.c2p(b, 0));
    const c = o.color || "BLUE";
    return new Poly(pts, {closed: true, color: null, fill: c, fop: o.fop ?? 0.3});
  }
}
class Graph extends Shape {      // courbe d'une fonction f(x, k) ; k animable : g.animate.set({k: 2})
  constructor(ax, f, o = {}) {
    super(Object.assign({color: "BLUE", width: 4}, o)); this._args = [ax, f, o];
    this.ax = ax; this.f = f; this.k = o.k; this.xr = o.x || [ax.xr[0], ax.xr[1]]; this.n = o.samples || 240;
  }
  extra() { return Object.assign(super.extra(), {k: this.k}); }
  pts() {
    const out = [], yl = this.ax.yr, m = (yl[1] - yl[0]) * 0.02;
    for (let i = 0; i <= this.n; i++) {
      const x = this.xr[0] + (this.xr[1] - this.xr[0]) * i / this.n, y = this.f(x, this.k);
      out.push(Number.isFinite(y) && y >= yl[0] - m && y <= yl[1] + m ? this.ax.c2p(x, y) : null);
    }
    return out;
  }
  d() { let s = "", pen = false; for (const p of this.pts()) { if (!p) { pen = false; continue; } s += (pen ? "L " : "M ") + px(p).map(f2).join(" ") + " "; pen = true; } return s; }
  lb() { const ps = this.pts().filter(Boolean); if (!ps.length) return {x0: 0, y0: 0, x1: 0, y1: 0}; return {x0: Math.min(...ps.map(p => p[0])), y0: Math.min(...ps.map(p => p[1])), x1: Math.max(...ps.map(p => p[0])), y1: Math.max(...ps.map(p => p[1]))}; }
  bbox() { return this.lb(); }
  render() { this.el.style.opacity = Math.max(0, Math.min(1, this.op)); this.draw(); }
  shift() { return this; } scale() { return this; }
  point_at(t) { const x = this.xr[0] + (this.xr[1] - this.xr[0]) * t; return this.ax.c2p(x, this.f(x, this.k)); }
  at_x(x) { return this.ax.c2p(x, this.f(x, this.k)); }
  segments() { const s = [], ps = this.pts(); for (let i = 1; i < ps.length; i += 4) if (ps[i] && ps[i - 1]) s.push([ps[i - 1], ps[Math.min(ps.length - 1, i + 3)] || ps[i]]); return s; }
}

// --- plan avec grille, que ApplyMatrix déforme (algèbre linéaire à la 3Blue1Brown) ---
class Plane extends Group {
  constructor(o = {}) {
    const xr = o.x || [-7, 7], yr = o.y || [-4, 4], u = o.unit || 1, at = o.at || ORIGIN;
    const self = {M: [1, 0, 0, 1]};
    const map = v => { const M = self.M; return [at[0] + (M[0] * v[0] + M[1] * v[1]) * u, at[1] + (M[2] * v[0] + M[3] * v[1]) * u]; };
    const bg = [], grid = [];
    const raw = v => [at[0] + v[0] * u, at[1] + v[1] * u];
    const ext = 3;   // la grille déformée dépasse le cadre
    if (o.background !== false) {
      for (let x = Math.ceil(xr[0]); x <= xr[1]; x++) bg.push(new Line(raw([x, yr[0]]), raw([x, yr[1]]), {width: 1, color: "GREY", bg: true}));
      for (let y = Math.ceil(yr[0]); y <= yr[1]; y++) bg.push(new Line(raw([xr[0], y]), raw([xr[1], y]), {width: 1, color: "GREY", bg: true}));
      bg.forEach(l => l.op = 0.25);
    }
    for (let x = Math.ceil(xr[0]) - ext; x <= xr[1] + ext; x++) if (x) grid.push(new Line(() => map([x, yr[0] - ext]), () => map([x, yr[1] + ext]), {width: 1.5, color: o.color || "BLUE", bg: true}));
    for (let y = Math.ceil(yr[0]) - ext; y <= yr[1] + ext; y++) if (y) grid.push(new Line(() => map([xr[0] - ext, y]), () => map([xr[1] + ext, y]), {width: 1.5, color: o.color || "BLUE", bg: true}));
    const ax = [new Line(() => map([xr[0] - ext, 0]), () => map([xr[1] + ext, 0]), {width: 2.5, color: "FG", bg: true}),
                new Line(() => map([0, yr[0] - ext]), () => map([0, yr[1] + ext]), {width: 2.5, color: "FG", bg: true})];
    super(...bg, ...grid, ...ax); this._args = [o];
    this.self = self; this.u = u; this.at0 = at; this.xr = xr; this.yr = yr; this.grid = new Group(...grid, ...ax); this.bgGrid = new Group(...bg);
  }
  get M() { return this.self.M; } set M(v) { this.self.M = v; }
  state() { return {M: this.self.M.slice(), kids: this.kids.map(k => k.state())}; }
  setState(st) { if (st.M) this.self.M = st.M.slice(); return super.setState(st); }
  bbox() { const F = S.frame(); return F; }
  c2p(x, y) { const M = this.self.M; return [this.at0[0] + (M[0] * x + M[1] * y) * this.u, this.at0[1] + (M[2] * x + M[3] * y) * this.u]; }
  raw(x, y) { return [this.at0[0] + x * this.u, this.at0[1] + y * this.u]; }
  shift() { return this; } scale() { return this; }
  vector(v, o = {}) {      // flèche de l'origine à M·v, qui suit les transformations
    const a = Arrow(() => this.c2p(0, 0), () => this.c2p(v[0], v[1]), Object.assign({color: "YELLOW", width: 5}, o));
    a.v = v;
    if (o.label) {
      const lab = isMob(o.label) ? o.label : Tex(o.label, {color: o.color || "YELLOW", size: 0.9});
      const place = () => { const tip = this.c2p(v[0], v[1]), d = unit(sub(tip, this.c2p(0, 0))); lab.move_to(add(tip, mul(d, 0.25 + Math.max(lab.width, lab.height) / 2))); };
      place(); lab.add_updater(place); a.lab = lab;
    }
    return a;
  }
  basis() { return [this.vector([1, 0], {color: "GREEN", label: "\\hat{\\imath}"}), this.vector([0, 1], {color: "RED", label: "\\hat{\\jmath}"})]; }
  dot(v, o = {}) { const d = new Dot(this.c2p(v[0], v[1]), o); d.add_updater(m => m.move_to(this.c2p(v[0], v[1]))); return d; }
}

// --- réseau de neurones : couches de nœuds, arêtes calculées, étiquettes de poids ---
class Network extends Group {
  constructor(layers, o = {}) {
    const w = o.w || 9, h = o.h || 4.5, at = o.at || ORIGIN, r = o.r || 0.42, L = layers.length;
    const nodes = {}, order = [], edges = {}, elist = [];
    layers.forEach((layer, i) => {
      const n = layer.length, gap = n > 1 ? Math.min(h / (n - 1), o.gap || 2) : 0;
      layer.forEach((spec, j) => {
        const sp = typeof spec === "string" ? {id: spec} : spec;
        const nd = new Node(sp.tex ?? sp.id, {r: sp.r || r, color: sp.color || o.color || "BLUE", name: sp.name, size: sp.size, name_dir: sp.name_dir});
        nd.move_to([at[0] - w / 2 + (L > 1 ? i * w / (L - 1) : w / 2), at[1] + (n - 1) / 2 * gap - j * gap]);
        nd.id = sp.id; nodes[sp.id] = nd; order.push(nd);
      });
    });
    const pairs = o.edges || layers.slice(1).flatMap((ly, i) => layers[i].flatMap(a => ly.map(b => [typeof a === "string" ? a : a.id, typeof b === "string" ? b : b.id])));
    for (const [a, b] of pairs) {
      const e = new Line(nodes[a], nodes[b], {width: 2.5, color: o.edge_color || "GREY", buff: 0.04});
      e.from = a; e.to = b; edges[a + ">" + b] = e; elist.push(e);
    }
    super(...elist, ...order); this._args = [layers, o];
    this.nodes = nodes; this.edges = edges; this.elist = elist; this.order = order;
  }
  node(id) { return this.nodes[id]; }
  edge(a, b) { return this.edges[a + ">" + b]; }
  edges_from(id) { return new Group(...this.elist.filter(e => e.from === id)); }
  edges_to(id) { return new Group(...this.elist.filter(e => e.to === id)); }
  // poids écrit sur l'arête, près du départ (t = 0.3) : les arêtes qui se croisent au milieu ne se gênent pas
  // posé SUR l'arête, dans une pastille (side: 1 ou -1 pour le mettre à côté)
  weight(a, b, tex, o = {}) { return this.edge(a, b).label(tex, Object.assign({t: 0.3, side: 0, size: 0.72, color: "BLUE"}, o)); }
}

// ---------- animations ----------
const smooth = t => t * t * t * (t * (t * 6 - 15) + 10), linear = t => t, there_and_back = t => smooth(t < 0.5 ? 2 * t : 2 - 2 * t);
const RATES = {smooth, linear, there_and_back};
function interp(a, b, t) {
  if (t <= 0) return a; if (t >= 1) return b;
  if (typeof a === "number" && typeof b === "number") return lerp(a, b, t);
  if (Array.isArray(a) && Array.isArray(b)) return a.length === b.length ? a.map((v, i) => interp(v, b[i], t)) : (t < 0.5 ? a : b);
  if (isColor(a) && isColor(b)) { const ca = rgb(a), cb = rgb(b); return hex(ca.map((v, i) => lerp(v, cb[i], t))); }
  if (a && b && typeof a === "object" && typeof b === "object") { const o = {}; for (const k in b) o[k] = k in a ? interp(a[k], b[k], t) : b[k]; return o; }
  return t < 0.5 ? a : b;
}
class Anim {
  constructor(mob, o = {}) { this.mob = mob; this.d = o.time ?? 1; this.rate = RATES[o.rate] || o.rate || smooth; this.begun = false; this.done = false; }
  duration() { return this.d; }
  scaleTo(T) { this.d = T; }
  start() { if (!this.begun) { this.begun = true; this.begin(); } }
  update(e) { this.start(); const a = this.d > 0 ? Math.min(1, Math.max(0, e / this.d)) : 1; this.at(this.rate(a)); }
  finish() { this.start(); if (!this.done) { this.done = true; this.at(1); this.end(); } }
  begin() {} at() {} end() {}
}
class Tween extends Anim {        // de l'état « from » à l'état « to », préparés au début de l'animation
  constructor(mob, prep, o = {}) { super(mob, o); this.prep = prep; this.o = o; }
  begin() { const r = this.prep(this.mob); this.from = r.from; this.to = r.to; this.after = r.end; this.onAt = r.at; }
  at(a) { this.mob.setState(interp(this.from, this.to, a)); if (this.onAt) this.onAt(a); }
  end() { if (this.after) this.after(); }
}
class Par extends Anim {          // plusieurs animations, décalées (LaggedStart) ou ensemble
  constructor(anims, offs, o = {}) { super(null, o); this.anims = anims; this.offs = offs; }
  duration() { return Math.max(0, ...this.anims.map((a, i) => this.offs[i] + a.duration())); }
  scaleTo(T) { const D = this.duration() || 1, f = T / D; this.offs = this.offs.map(x => x * f); this.anims.forEach(a => a.scaleTo(a.duration() * f)); }
  start() { this.begun = true; }
  update(e) { this.anims.forEach((a, i) => { if (e >= this.offs[i]) a.update(e - this.offs[i]); }); }
  finish() { this.anims.forEach(a => a.finish()); this.done = true; }
}
function animBuilder(mob) {   // m.animate.shift(UP).set_color(RED) : enregistre les appels, rejoués au début
  const calls = [];
  const anim = new Tween(mob, m => { const from = m.state(); calls.forEach(([k, a]) => m[k](...a)); const to = m.state(); m.setState(from); return {from, to}; });
  const prox = new Proxy(anim, {get(t, k) {
    if (k in t || typeof k === "symbol") return t[k];
    if (typeof mob[k] !== "function") throw new Error(`.animate.${String(k)}() : ${mob.constructor.name} n'a pas de méthode ${String(k)} (voir API.md, « Placement »)`);
    return (...a) => { calls.push([k, a]); return prox; };
  }});
  return prox;
}
const leavesOf = m => m.leaves();
function Create(m, o = {}) {
  const ls = leavesOf(m), n = ls.length, lag = o.lag ?? (n > 1 ? Math.min(0.08, 0.6 / n) : 0);
  const one = l => new Tween(l, x => {
    S.add(x); const to = x.state(); x.draw_t = 0; if (x instanceof TexMob) x.op = 0; const from = x.state(); x.setState(from); return {from, to};
  }, {time: o.time ?? 1, rate: o.rate});
  const anims = ls.map(one);
  return n === 1 ? anims[0] : new Par(anims, anims.map((_, i) => i * lag), o);
}
const Write = Create;
function FadeIn(m, o = {}) {
  return new Tween(m, x => {
    S.add(x); const to = x.state();
    x.set_opacity(0); if (o.shift) x.shift(mul(o.shift, -1)); if (o.scale) x.scale(o.scale);
    const from = x.state(); x.setState(from); return {from, to};
  }, o);
}
function FadeOut(m, o = {}) {
  let keep;
  return new Tween(m, x => {
    const from = x.state(); keep = from;
    x.set_opacity(0); if (o.shift) x.shift(o.shift); if (o.scale) x.scale(o.scale);
    const to = x.state(); x.setState(from);
    return {from, to, end: () => { S.remove(x); x.setState(keep); }};
  }, o);
}
function Uncreate(m, o = {}) {
  return new Tween(m, x => {
    const from = x.state(); x.leaves().forEach(l => { if ("draw_t" in l) l.draw_t = 0; l.op = l instanceof TexMob ? 0 : l.op; });
    const to = x.state(); x.setState(from);
    return {from, to, end: () => { S.remove(x); x.setState(from); }};
  }, o);
}
// a vole jusqu'à b et devient b (copy : a reste en place, c'est une copie qui vole)
function morph(a, b, o, keepA) {
  let c, bTo;
  return new Tween(a, () => {
    c = a.copy(); S.add(c); if (!keepA) S.remove(a);
    S.add(b); bTo = b.state();
    const cFrom = c.state();
    const k = Math.max(b.width, b.height) / Math.max(a.width, a.height, 1e-3);
    c.scale(k).move_to(b.center()); if (o.color || (b.color && !(b instanceof Group))) c.set_color(o.color || b.color);
    const cTo = c.state(); c.setState(cFrom); b.set_opacity(0);
    return {from: {}, to: {}, at: t => {
      c.setState(interp(cFrom, cTo, t)); c.leaves().forEach(l => l.op *= 1 - smooth(Math.min(1, Math.max(0, (t - 0.55) / 0.45))));
      b.setState(bTo); b.leaves().forEach(l => l.op *= smooth(Math.min(1, Math.max(0, (t - 0.45) / 0.55))));
    }, end: () => { S.remove(c); b.setState(bTo); }};
  }, Object.assign({time: 1.2}, o));
}
const Transform = (a, b, o = {}) => morph(a, b, o, false);
const TransformFromCopy = (a, b, o = {}) => morph(a, b, o, true);
function ChangeTex(t, tex, o = {}) {      // remplace le contenu d'une formule sur place (ancien vers le haut, nouveau du bas)
  let old;
  return new Tween(t, () => {
    const on = S.has(t); old = t.copy(); if (on) S.add(old);
    const c = t.center(); t.tex = String(tex); if (o.color) t.color = o.color; t.remeasure(); t.move_to(c);
    const to = t.state(); const oFrom = old.state(), oTo = Object.assign({}, oFrom, {op: 0, y: oFrom.y + 0.25});
    t.op = 0; t.y -= 0.25; const from = t.state(); if (!on) { t.setState(to); return {from: to, to}; }
    return {from, to, at: a => old.setState(interp(oFrom, oTo, a)), end: () => S.remove(old)};
  }, Object.assign({time: 0.8}, o));
}
function Indicate(m, o = {}) {
  return new Tween(m, x => {
    const from = x.state(); x.scale(o.scale || 1.2); x.set_color(o.color || "YELLOW"); const mid = x.state(); x.setState(from);
    return {from, to: mid, end: () => x.setState(from)};
  }, Object.assign({rate: there_and_back}, o));
}
function Circumscribe(m, o = {}) {
  let r;
  return new Tween(m, () => {
    r = SurroundingRect(m, o); S.add(r); r.draw_t = 0;
    return {from: {}, to: {}, at: a => { r.draw_t = Math.min(1, a * 2); r.op = a < 0.6 ? 1 : 1 - (a - 0.6) / 0.4; }, end: () => S.remove(r)};
  }, Object.assign({time: 1.5, rate: linear}, o));
}
// un point lumineux parcourt un segment ou une courbe (propagation d'un signal) ; paint : le segment garde la couleur
function Flow(line, o = {}) {
  let g, c0, w0;
  return new Tween(line, l => {
    const c = o.color || "YELLOW";
    g = new Group(new Circle({r: 0.2, color: null, fill: c, fop: 0.3}), new Circle({r: 0.09, color: null, fill: c, fop: 1}));
    S.add(g); c0 = l.color; w0 = l.sw;
    return {from: {}, to: {}, at: a => {
      const t = o.reverse ? 1 - a : a; g.move_to(l.point_at(t)); g.kids[0].op = g.kids[1].op = Math.min(1, a * 8, (1 - a) * 8);
      if (o.paint) { l.color = interp(c0, c, a); l.sw = lerp(w0, w0 * 1.6, a); }
    }, end: () => { S.remove(g); if (o.paint) { l.color = c; l.sw = w0 * 1.6; } }};
  }, Object.assign({time: 1.2, rate: linear}, o));
}
function ApplyMatrix(plane, A, o = {}) {
  return new Tween(plane, p => {
    const from = p.state(), M = p.self.M;
    const to = Object.assign({}, from, {M: [A[0][0] * M[0] + A[0][1] * M[2], A[0][0] * M[1] + A[0][1] * M[3], A[1][0] * M[0] + A[1][1] * M[2], A[1][0] * M[1] + A[1][1] * M[3]]});
    return {from: {M: from.M}, to: {M: to.M}};
  }, Object.assign({time: 2}, o));
}
const LaggedStart = (...args) => {
  const o = (args.length && !(args[args.length - 1] instanceof Anim)) ? args.pop() : {};
  const anims = args.flat(), lag = o.lag ?? 0.25;
  return new Par(anims, anims.map((a, i) => i * lag * (anims[0] ? anims[0].duration() : 1)), o);
};
const AnimationGroup = (...anims) => new Par(anims.flat(), anims.flat().map(() => 0));

// ---------- scène ----------
class Scene {
  constructor(o) {
    this.W = o.w || 16; this.H = o.h || 9; this.root = null; this.on = new Set(); this.mobs = []; this.steps = []; this.q = null;
  }
  frame() { return {x0: -this.W / 2, y0: -this.H / 2, x1: this.W / 2, y1: this.H / 2}; }
  has(m) { return m.leaves().every(l => this.on.has(l)); }
  add(...ms) {
    for (const m of ms.flat()) {
      if (!this.mobs.includes(m)) this.mobs.push(m);
      for (const l of m.leaves()) { if (!this.on.has(l)) { this.on.add(l); (l.html_ ? this.ov : this.root).append(l.node); } }
      m.render();
    }
    return ms[0];
  }
  remove(...ms) {
    for (const m of ms.flat()) {
      for (const l of m.leaves()) { this.on.delete(l); l.node.remove(); }
      this.mobs = this.mobs.filter(x => x !== m);
    }
  }
  bring_to_front(...ms) { for (const m of ms.flat()) for (const l of m.leaves()) if (this.on.has(l)) (l.html_ ? this.ov : this.root).append(l.node); }
  play(...args) {
    const o = (args.length && !(args[args.length - 1] instanceof Anim)) ? args.pop() : {};
    const anims = args.flat().filter(Boolean);
    const p = new Par(anims, anims.map(() => 0));
    if (o.time) p.scaleTo(o.time);
    if (o.rate) anims.forEach(a => a.rate = RATES[o.rate] || o.rate);
    this.queue({kind: "play", anim: p});
  }
  wait(t = 1) { this.queue({kind: "wait", t}); }
  do(fn) { this.queue({kind: "do", fn}); }
  queue(a) { if (this.q) this.q.push(a); else this.runNow(a); }
  runNow(a) { if (a.kind === "play") a.anim.finish(); else if (a.kind === "do") a.fn(); this.tick(0); }
  geom() {            // du repère du SVG (viewBox, « meet ») aux pixels du calque de textes
    const sv = document.getElementById("sc"), r = sv.getBoundingClientRect(), wr = this.ov.parentNode.getBoundingClientRect();
    const k = Math.min(r.width / (this.W * U), r.height / (this.H * U)) || 1;
    this.k = k; this.ox = r.left - wr.left + (r.width - this.W * U * k) / 2; this.oy = r.top - wr.top + (r.height - this.H * U * k) / 2;
    const st = this.ov.style; st.left = f2(this.ox) + "px"; st.top = f2(this.oy) + "px";
    st.width = f2(this.W * U * k) + "px"; st.height = f2(this.H * U * k) + "px";
  }
  step(caption, fn) { if (typeof caption === "function") { fn = caption; caption = ""; } this.steps.push({caption, fn}); }
  // un pas de rendu : mises à jour attachées, puis dessin de tout ce qui est affiché
  tick(dt) {
    this.geom();
    let err = null;
    for (const l of [...this.on]) for (const u of l.ups) { try { u(l, dt); } catch (e) { err = err || e; } }
    for (const l of this.on) { try { l.render(); } catch (e) { err = err || e; } }
    if (err) throw err;
  }
}

let BUILD = null, OPTS = {}, cur = -1, token = 0, ready = false, running = false, SPEED = 1;
function scene(o, fn) { if (typeof o === "function") { fn = o; o = {}; } OPTS = o; BUILD = fn; if (ready) goto(0); }

function rebuild() {
  S = new Scene(OPTS);
  const sv = document.getElementById("sc");
  sv.setAttribute("viewBox", `0 0 ${S.W * U} ${S.H * U}`);
  sv.innerHTML = "";
  const cp = svg("clipPath", {id: "sc-clip"}); cp.append(svg("rect", {x: 0, y: 0, width: S.W * U, height: S.H * U}));
  const defs = svg("defs"); defs.append(cp); sv.append(defs);
  const clip = svg("g", {"clip-path": "url(#sc-clip)"}); sv.append(clip);
  S.root = svg("g", {transform: `translate(${S.W * U / 2} ${S.H * U / 2})`}); clip.append(S.root);
  S.ov = document.getElementById("sc-ov"); S.ov.innerHTML = ""; S.geom();
  try { BUILD(S); S.tick(0); }
  catch (e) { showError(e); throw e; }
}
function showError(e) {
  const msg = e && e.message ? `${e.message} (${String(e.stack || "").split("\n")[0]})` : String(e);
  (window.__sceneErrors = window.__sceneErrors || []).push(msg);
  // jusqu'à la page du prof (fenêtre du haut), qui le signale au prof : il ne voit pas l'écran de l'élève
  try { if (window.top !== window) window.top.postMessage({sceneError: msg, scene: document.title || (S && S.steps[0] && S.steps[0].caption) || ""}, "*"); } catch (x) {}
  const b = document.getElementById("sc-err"); b.textContent = "Erreur dans la scène : " + (e && e.message || e); b.style.display = "block";
}
async function runStep(k, animate, tk) {
  const st = S.steps[k]; if (!st) return;
  S.q = []; try { st.fn(); } catch (e) { showError(e); } const q = S.q; S.q = null;
  setCaption(k);
  if (animate) running = true;
  for (const a of q) {
    if (tk !== token) return;                 // une autre étape a été demandée : cette scène est abandonnée
    if (!animate) { S.runNow(a); continue; }
    if (a.kind === "do") { try { a.fn(); S.tick(0); } catch (e) { showError(e); } continue; }
    const D = (a.kind === "wait" ? a.t : a.anim.duration()) / SPEED;
    try { if (a.kind === "play") a.anim.update(0); } catch (e) { showError(e); }
    await new Promise(res => {
      const t0 = performance.now();
      const frame = now => {
        if (tk !== token) return res();
        const e = (now - t0) / 1000;
        try {            // une erreur ne fige pas la scène : l'animation est terminée d'un coup, et l'erreur affichée
          if (a.kind === "play") { if (e >= D) a.anim.finish(); else a.anim.update(e * SPEED); }
          S.tick(1 / 60);
        } catch (err) {
          showError(err);
          try { if (a.kind === "play") a.anim.finish(); S.tick(0); } catch (e2) {}
          return res();
        }
        if (e >= D) res(); else requestAnimationFrame(frame);
      };
      requestAnimationFrame(frame);
    });
  }
  if (tk === token) running = false;
}
async function goto(k, instant = false) {
  if (!BUILD) return;
  const n = S ? S.steps.length : 0;
  const tk = ++token;
  if (!(S && !instant && !running && k === cur + 1 && k < n)) {
    running = false;
    rebuild();
    for (let i = 0; i < Math.min(k, S.steps.length); i++) await runStep(i, false, tk);
  }
  cur = Math.max(-1, Math.min(k, S.steps.length - 1));
  setCaption(cur);
  if (k >= 0 && k < S.steps.length) await runStep(k, !instant, tk);
}
function setCaption(k) {
  const n = S.steps.length, bar = document.getElementById("sc-bar");
  bar.style.display = n ? "flex" : "none";
  document.getElementById("sc-k").textContent = n ? `${Math.max(0, k) + 1} / ${n}` : "";
  const cap = k >= 0 && S.steps[k] ? S.steps[k].caption : "";
  document.getElementById("sc-cap").innerHTML = cap.split(/(\$[^$]+\$)/).map(p => p.startsWith("$") && p.length > 2 ? renderMath(p.slice(1, -1)) : esc(p)).join("");
}

// ---------- vérification (scene.py) : chevauchements, débordements ----------
function report() {
  const vis = [...S.on].filter(l => l.op > 0.05 && l.node.isConnected);
  const texts = vis.filter(l => l instanceof TexMob && l.tex.trim());
  const name = t => "« " + (t.tex.length > 28 ? t.tex.slice(0, 26) + "…" : t.tex) + " »";
  const inter = (a, b) => Math.max(0, Math.min(a.x1, b.x1) - Math.max(a.x0, b.x0)) * Math.max(0, Math.min(a.y1, b.y1) - Math.max(a.y0, b.y0));
  const area = b => (b.x1 - b.x0) * (b.y1 - b.y0);
  const shrink = (b, k) => { const dx = (b.x1 - b.x0) * k, dy = (b.y1 - b.y0) * k; return {x0: b.x0 + dx, y0: b.y0 + dy, x1: b.x1 - dx, y1: b.y1 - dy}; };
  const out = [];
  for (let i = 0; i < texts.length; i++) for (let j = i + 1; j < texts.length; j++) {
    const a = texts[i].bbox(), b = texts[j].bbox(), I = inter(a, b);
    if (I > 0.12 * Math.min(area(a), area(b))) out.push(`texte ${name(texts[i])} chevauche ${name(texts[j])}`);
  }
  const segHits = (b, p, q) => {      // le segment pq traverse-t-il la boîte b ?
    let t0 = 0, t1 = 1; const d = sub(q, p);
    for (const [pp, dd, lo, hi] of [[p[0], d[0], b.x0, b.x1], [p[1], d[1], b.y0, b.y1]]) {
      if (Math.abs(dd) < 1e-12) { if (pp < lo || pp > hi) return false; continue; }
      let a = (lo - pp) / dd, c = (hi - pp) / dd; if (a > c) [a, c] = [c, a];
      t0 = Math.max(t0, a); t1 = Math.min(t1, c); if (t0 > t1) return false;
    }
    return true;
  };
  const lines = vis.filter(l => l.segments && !l.bg);
  for (const t of texts) {
    const b = shrink(t.bbox(), 0.18);
    for (const l of lines) {
      if (t.owner === l) continue;
      if (l.segments().some(([p, q]) => segHits(b, p, q))) { out.push(`un trait (${l.constructor.name === "Line" ? (l.tip ? "flèche" : "segment") : "courbe"}) traverse le texte ${name(t)}`); break; }
    }
    for (const s of vis) {
      if (!(s instanceof Circle || s instanceof Rect) || s.op < 0.05) continue;
      const sb = s.bbox(), c = t.center();
      const inside = c[0] > sb.x0 && c[0] < sb.x1 && c[1] > sb.y0 && c[1] < sb.y1;
      if (!inside && inter(b, sb) > 0.05 * area(b) && !(s.r && s.r < 0.12)) { out.push(`le texte ${name(t)} déborde sur une forme (${s instanceof Circle ? "cercle" : "case"}) sans être dedans`); break; }
    }
  }
  const F = S.frame(), m = 0.05;
  for (const l of vis) {
    if (l.bg) continue;
    const b = l.bbox();
    if (b.x0 < F.x0 - m || b.x1 > F.x1 + m || b.y0 < F.y0 - m || b.y1 > F.y1 + m)
      out.push(`${l instanceof TexMob ? "le texte " + name(l) : "un objet (" + l.constructor.name + ")"} sort du cadre (cadre : x de ${F.x0} à ${F.x1}, y de ${F.y0} à ${F.y1})`);
  }
  const words = texts.filter(t => t.text).reduce((n, t) => n + t.tex.replace(/\$[^$]*\$/g, "x").split(/\s+/).filter(Boolean).length, 0);
  if (words > 25) out.push(`beaucoup de texte dans le dessin (${words} mots) : laisse le dessin parler, le reste à l'oral`);
  const hints = [];
  const content = vis.filter(l => !l.bg);
  if (vis.some(l => l.bg)) { /* un plan remplit le cadre : pas de conseil de place */ }
  else if (content.length) {
    const bs = content.map(l => l.bbox()), F2 = S.frame();
    const y0 = Math.max(F2.y0, Math.min(...bs.map(b => b.y0))), y1 = Math.min(F2.y1, Math.max(...bs.map(b => b.y1)));
    const x0 = Math.max(F2.x0, Math.min(...bs.map(b => b.x0))), x1 = Math.min(F2.x1, Math.max(...bs.map(b => b.x1)));
    if (y1 - y0 < 0.6 * S.H) hints.push(`le dessin n'occupe que ${f2(y1 - y0)} des ${S.H} unités de hauteur (y de ${f2(y0)} à ${f2(y1)}) : réduis h dans scene({h: …}) ou centre le dessin`);
    if (x1 - x0 < 0.55 * S.W) hints.push(`le dessin n'occupe que ${f2(x1 - x0)} des ${S.W} unités de largeur : agrandis-le ou réduis w`);
  } else hints.push("rien de visible à cette étape");
  return {problems: [...new Set(out)], hints};
}

// ---------- page ----------
const CSS = `
html,body{margin:0;height:100%;overflow:hidden}
#sc-wrap{display:flex;flex-direction:column;height:100vh;position:relative}
#sc-ov{position:absolute;overflow:hidden;pointer-events:none}
#sc-ov .sc-tex{position:absolute;pointer-events:auto;transform-origin:50% 50%}
:where(:root){--bg:#fff;--fg:#1f1e1c;--muted:#6d6b65;--line:#d8d6ce}
@media (prefers-color-scheme:dark){:where(:root){--bg:#262624;--fg:#ecebe6;--muted:#a3a19a;--line:#4a4945}}
body{color:var(--fg)}
#sc{flex:1;min-height:0;width:100%;display:block}
#sc-bar{display:none;align-items:center;gap:8px;padding:4px 8px;font:15px system-ui,sans-serif;min-height:34px}
#sc-bar button{padding:2px 10px}
#sc-k{color:var(--muted);font-size:13px;min-width:3em}
#sc-cap{flex:1}
#sc-err{display:none;color:#e5534b;font:14px monospace;padding:6px 10px;white-space:pre-wrap}
.sc-tex{line-height:1.25;font-family:KaTeX_Main,"Latin Modern Roman",serif}
.sc-tex .katex{font-size:1.1em}
#sc-meas{position:absolute;left:-9999px;top:0;visibility:hidden}`;
function setup() {
  const st = document.createElement("style"); st.textContent = CSS; document.head.append(st);
  document.body.insertAdjacentHTML("beforeend", `<div id="sc-wrap"><svg id="sc" preserveAspectRatio="xMidYMid meet"></svg><div id="sc-ov"></div>
    <div id="sc-err"></div><div id="sc-bar"><button id="sc-prev" title="Étape précédente">◀</button><span id="sc-k"></span>
    <button id="sc-next" title="Étape suivante">▶</button><button id="sc-again" title="Rejouer l'étape">↻</button><span id="sc-cap"></span></div></div>
    <div id="sc-meas"></div>`);
  document.getElementById("sc-prev").onclick = () => goto(Math.max(0, cur - 1));
  document.getElementById("sc-next").onclick = () => goto(Math.min(S.steps.length - 1, cur + 1));
  document.getElementById("sc-again").onclick = () => goto(cur);
  addEventListener("message", e => { if (e.data && typeof e.data.step === "number") goto(e.data.step); });
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => goto(cur, true));
  let rz = 0;
  new ResizeObserver(() => { cancelAnimationFrame(rz); rz = requestAnimationFrame(() => { try { S && S.ov && S.tick(0); } catch (e) { showError(e); } }); })
    .observe(document.getElementById("sc"));
}
async function fontsReady() {
  const faces = ["20px KaTeX_Main", "italic 20px KaTeX_Main", "bold 20px KaTeX_Main", "italic 20px KaTeX_Math", "20px KaTeX_AMS",
                 "20px KaTeX_Size1", "20px KaTeX_Size2", "20px KaTeX_Size3", "20px KaTeX_Size4"];
  try { await Promise.race([Promise.all(faces.map(f => document.fonts.load(f))), new Promise(r => setTimeout(r, 4000))]); } catch (e) {}
  try { await document.fonts.ready; } catch (e) {}
}
window.addEventListener("error", e => {
  const m = String(e.message || "");
  if (/ResizeObserver loop/.test(m)) return;
  if (!e.error && /^Script error\.?$/.test(m)) { console.warn("erreur masquée par le navigateur (Script error.)"); return; }
  showError(e.error || m);
});
window.addEventListener("unhandledrejection", e => showError(e.reason));
(async () => {
  if (document.readyState === "loading") await new Promise(r => document.addEventListener("DOMContentLoaded", r));
  setup();
  for (let i = 0; i < 100 && typeof katex === "undefined"; i++) await new Promise(r => setTimeout(r, 50));
  if (typeof katex === "undefined") showError("KaTeX n'a pas chargé (réseau ?)");
  await fontsReady();
  ready = true;
  if (BUILD) goto(0, !!window.__SCENE_CHECK);
})();

async function playthrough(speed = 4) {
  SPEED = speed;
  try { for (let k = 0; k < (S ? S.steps.length : 0); k++) await goto(k); }
  finally { SPEED = 1; }
  return window.__sceneErrors || [];
}
window.__scene = {
  goto, report, playthrough, steps: () => (S ? S.steps.length : 0), ready: () => ready && !!S,
  size: () => [OPTS.w || 16, OPTS.h || 9], captions: () => S.steps.map(s => s.caption), errors: () => window.__sceneErrors || [],
};
// tout s'appelle avec ou sans new : Group(a, b) comme new Group(a, b)
const exported = {
  scene, UP, DOWN, LEFT, RIGHT, ORIGIN, UL, UR, DL, DR,
  BLUE: "BLUE", TEAL: "TEAL", GREEN: "GREEN", YELLOW: "YELLOW", GOLD: "GOLD", RED: "RED", MAROON: "MAROON",
  PURPLE: "PURPLE", PINK: "PINK", ORANGE: "ORANGE", GREY: "GREY", FG: "FG", MUTED: "MUTED",
  Group: (...m) => new Group(...m), Circle: o => new Circle(o), Dot: (p, o) => new Dot(p, o), Rect: o => new Rect(o),
  Polygon: (pts, o = {}) => new Poly(pts, Object.assign({closed: true}, o)), Polyline: (pts, o) => new Poly(pts, o),
  Line: (a, b, o) => new Line(a, b, o), Arrow, DoubleArrow, DashedLine, Tex, Text, Num: (v, o) => new Num(v, o),
  Node: (t, o) => new Node(t, o), Box: (t, o) => new Box(t, o), Matrix: (r, o) => new Matrix(r, o), Table: (r, o) => new Table(r, o),
  Brace: (m, d, o) => new Brace(m, d, o), SurroundingRect, Axes: o => new Axes(o), Plane: o => new Plane(o),
  Network: (l, o) => new Network(l, o),
  Create, Write, Uncreate, FadeIn, FadeOut, Transform, TransformFromCopy, ChangeTex, Indicate, Circumscribe, Flow,
  ApplyMatrix, LaggedStart, AnimationGroup,
  add, sub, mul, unit,
};
for (const k in exported) {
  const f = exported[k];
  window[k] = typeof f === "function" && k !== "scene" ? function (...a) { return f(...a); } : f;
}
})();
