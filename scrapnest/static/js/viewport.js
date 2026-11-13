// 図の拡大縮小と移動。world 座標（mm または写真の画素）と画面の画素を行き来する。

export class Viewport {
  constructor(svgEl, worldEl) {
    this.svg = svgEl;
    this.world = worldEl;
    this.s = 1;          // 画面の画素 / world の長さ
    this.tx = 0;
    this.ty = 0;
    this.flipY = true;   // true: y が上向き（mm の図）、false: y が下向き（写真）
    this.bounds = null;
    this.listeners = new Set();
    this.spaceDown = false;
    this._pan = null;

    svgEl.addEventListener("wheel", (e) => {
      e.preventDefault();
      this.zoomAt(e.clientX, e.clientY, Math.exp(-e.deltaY * 0.0015));
    }, { passive: false });
    svgEl.addEventListener("contextmenu", (e) => e.preventDefault());
    svgEl.addEventListener("pointerdown", (e) => {
      if (e.button === 1 || e.button === 2 || (e.button === 0 && this.spaceDown)) {
        e.preventDefault();
        e.stopPropagation();
        this._pan = { x: e.clientX, y: e.clientY, tx: this.tx, ty: this.ty };
        svgEl.setPointerCapture(e.pointerId);
        svgEl.classList.add("pan");
      }
    }, true);
    svgEl.addEventListener("pointermove", (e) => {
      if (!this._pan) return;
      this.tx = this._pan.tx + (e.clientX - this._pan.x);
      this.ty = this._pan.ty + (e.clientY - this._pan.y);
      this.apply();
    });
    const endPan = (e) => {
      if (!this._pan) return;
      this._pan = null;
      svgEl.classList.remove("pan");
      try { svgEl.releasePointerCapture(e.pointerId); } catch (_) { /* すでに外れている */ }
    };
    svgEl.addEventListener("pointerup", endPan);
    svgEl.addEventListener("pointercancel", endPan);
    window.addEventListener("keydown", (e) => { if (e.code === "Space" && !isTyping(e)) { this.spaceDown = true; e.preventDefault(); } });
    window.addEventListener("keyup", (e) => { if (e.code === "Space") this.spaceDown = false; });
    new ResizeObserver(() => { if (this.bounds && !this._userMoved) this.fit(...this.bounds); }).observe(svgEl);
  }

  get panning() { return !!this._pan; }

  onChange(fn) { this.listeners.add(fn); return () => this.listeners.delete(fn); }

  apply() {
    this.world.setAttribute("transform", `matrix(${this.s} 0 0 ${this.flipY ? -this.s : this.s} ${this.tx} ${this.ty})`);
    for (const fn of this.listeners) fn();
  }

  // 画面の座標 → world
  toWorld(clientX, clientY) {
    const r = this.svg.getBoundingClientRect();
    const x = (clientX - r.left - this.tx) / this.s;
    const y = (clientY - r.top - this.ty) / (this.flipY ? -this.s : this.s);
    return [x, y];
  }

  // 画面で n 画素にあたる world の長さ
  px(n) { return n / this.s; }

  fit(minx, miny, maxx, maxy, pad = 28) {
    this.bounds = [minx, miny, maxx, maxy];
    this._userMoved = false;
    const r = this.svg.getBoundingClientRect();
    const w = Math.max(maxx - minx, 1e-6), h = Math.max(maxy - miny, 1e-6);
    this.s = Math.max(1e-6, Math.min((r.width - 2 * pad) / w, (r.height - 2 * pad) / h));
    const cx = (minx + maxx) / 2, cy = (miny + maxy) / 2;
    this.tx = r.width / 2 - this.s * cx;
    this.ty = r.height / 2 - (this.flipY ? -this.s : this.s) * cy;
    this.apply();
  }

  refit() { if (this.bounds) this.fit(...this.bounds); }

  zoomAt(clientX, clientY, factor) {
    const r = this.svg.getBoundingClientRect();
    const px = clientX - r.left, py = clientY - r.top;
    const ns = Math.min(4000, Math.max(0.02, this.s * factor));
    const k = ns / this.s;
    this.tx = px - (px - this.tx) * k;
    this.ty = py - (py - this.ty) * k;
    this.s = ns;
    this._userMoved = true;
    this.apply();
  }
}

export function isTyping(e) {
  const t = e.target;
  return t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT" || t.isContentEditable);
}
