function clipforge() {
  return {
    steps: ["Upload", "Rights", "Settings", "Processing", "Review"],
    step: "upload",
    file: null,
    uploading: false,
    uploadPct: 0,
    video: null,
    rightsChecked: false,
    platforms: [],
    // { platform_key: { mode: 'coverage'|'highlights', lengthS, clipCount } }
    platformCfg: {},
    captions: { enabled: true, language: "" },
    job: null,
    clips: [],
    busy: false,
    error: "",
    _poll: null,

    stepIndex() {
      return this.steps.findIndex(
        (s) => s.toLowerCase() === this.step.toLowerCase()
      );
    },

    async init() {
      try {
        const r = await fetch("/platforms");
        this.platforms = (await r.json()).platforms;
      } catch (e) {
        this.error = "Could not load platforms";
      }
    },

    reset() {
      if (this._poll) clearInterval(this._poll);
      Object.assign(this, {
        step: "upload", file: null, uploading: false, uploadPct: 0,
        video: null, rightsChecked: false, platformCfg: {},
        captions: { enabled: true, language: "" },
        job: null, clips: [], busy: false, error: "",
      });
    },

    pickFile(e) {
      this.file = e.target.files[0] || null;
    },

    async startUpload() {
      if (!this.file) return;
      this.uploading = true;
      this.uploadPct = 0;
      try {
        const init = await this.postJSON("/videos/upload/init", {
          filename: this.file.name,
          size_bytes: this.file.size,
        });
        const vid = init.video_id;
        const chunk = init.chunk_size;
        let offset = 0;
        while (offset < this.file.size) {
          const slice = this.file.slice(offset, offset + chunk);
          const buf = await slice.arrayBuffer();
          const end = offset + slice.size - 1;
          await fetch(`/videos/${vid}/upload/chunk?offset=${offset}`, {
            method: "PUT",
            headers: {
              "Content-Type": "application/octet-stream",
              "Content-Range": `bytes ${offset}-${end}/${this.file.size}`,
            },
            body: buf,
          });
          offset += slice.size;
          this.uploadPct = Math.round((offset / this.file.size) * 100);
        }
        this.video = await this.postJSON(`/videos/${vid}/upload/complete`, {});
        this.uploading = false;
        this.step = "rights";
        this.pollVideo();
      } catch (e) {
        this.uploading = false;
        this.error = "Upload failed: " + e.message;
      }
    },

    pollVideo() {
      if (this._poll) clearInterval(this._poll);
      this._poll = setInterval(async () => {
        try {
          const r = await fetch(`/videos/${this.video.id}`);
          this.video = await r.json();
          if (["ready", "failed"].includes(this.video.status)) {
            clearInterval(this._poll);
          }
        } catch (e) {}
      }, 1500);
    },

    async confirmRights() {
      try {
        this.video = await this.postJSON(
          `/videos/${this.video.id}/confirm-rights`,
          { confirmed: this.rightsChecked }
        );
        this.step = "settings";
      } catch (e) {
        this.error = e.message;
      }
    },

    togglePlatform(p) {
      if (p.key in this.platformCfg) {
        delete this.platformCfg[p.key];
      } else {
        // Default to "cover whole video" with a sensible per-format length.
        const want = p.is_vertical ? 40 : 180;
        const lengthS = Math.min(p.length_max_s, Math.max(p.length_min_s, want));
        this.platformCfg[p.key] = {
          mode: "coverage",
          lengthS,
          clipCount: p.is_vertical ? 5 : 3,
        };
      }
    },

    coverageEstimate(p) {
      const cfg = this.platformCfg[p.key];
      const dur = this.video?.duration_s;
      if (!cfg || !dur || !cfg.lengthS) return "";
      const n = Math.max(1, Math.ceil(dur / cfg.lengthS));
      return `≈ ${n} clips to cover the whole video`;
    },

    async createJob() {
      this.busy = true;
      try {
        const platforms = Object.keys(this.platformCfg);
        const clip_counts = {};
        const coverage_lengths = {};
        for (const k of platforms) {
          const cfg = this.platformCfg[k];
          if (cfg.mode === "coverage") coverage_lengths[k] = cfg.lengthS;
          else clip_counts[k] = cfg.clipCount;
        }
        const body = {
          platforms: platforms,
          clip_counts: clip_counts,
          coverage_lengths: coverage_lengths,
          captions: {
            enabled: this.captions.enabled,
            language: this.captions.language || null,
          },
        };
        this.job = await this.postJSON(`/videos/${this.video.id}/jobs`, body);
        this.step = "processing";
        this.pollJob();
      } catch (e) {
        this.error = e.message;
      } finally {
        this.busy = false;
      }
    },

    pollJob() {
      if (this._poll) clearInterval(this._poll);
      this._poll = setInterval(async () => {
        try {
          const r = await fetch(`/jobs/${this.job.id}`);
          this.job = await r.json();
          // Clips appear once rendering starts; pull them in as each finishes.
          if (!["queued", "transcribing", "analyzing"].includes(this.job.stage)) {
            await this.refreshClips();
          }
          if (this.job.status === "completed") {
            clearInterval(this._poll);
            await this.refreshClips();
            this.step = "review";
          } else if (this.job.status === "failed") {
            clearInterval(this._poll);
          }
        } catch (e) {}
      }, 1500);
    },

    // Merge server state into the existing clip objects instead of replacing
    // them, so edits typed in the review screen survive the polling.
    async refreshClips() {
      const r = await fetch(`/jobs/${this.job.id}/clips`);
      const fresh = await r.json();
      const byId = new Map(this.clips.map((c) => [c.id, c]));
      this.clips = fresh.map((f) => {
        const cur = byId.get(f.id);
        if (!cur) return f;
        Object.assign(cur, {
          status: f.status,
          has_file: f.has_file,
          has_thumbnail: f.has_thumbnail,
          has_srt: f.has_srt,
          error: f.error,
        });
        return cur;
      });
    },

    readyCount() {
      return this.clips.filter((c) => ["ready", "approved"].includes(c.status)).length;
    },

    async saveClip(c) {
      await this.patchClip(c);
    },

    async patchClip(c) {
      const body = {
        start_s: c.start_s,
        end_s: c.end_s,
        title: c.title,
        description: c.description,
        captions_enabled: c.captions_enabled,
      };
      const r = await fetch(`/clips/${c.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!r.ok) {
        this.error = (await r.json()).error?.message || "Save failed";
        return false;
      }
      return true;
    },

    async rerender(c) {
      if (!(await this.patchClip(c))) return;
      const r = await fetch(`/clips/${c.id}/render`, { method: "POST" });
      if (r.ok) {
        c.status = "rendering";
        this.watchClip(c);
      }
    },

    watchClip(c) {
      const iv = setInterval(async () => {
        const r = await fetch(`/jobs/${this.job.id}/clips`);
        const all = await r.json();
        const fresh = all.find((x) => x.id === c.id);
        if (fresh) Object.assign(c, fresh);
        if (!fresh || ["ready", "approved", "failed"].includes(fresh.status)) {
          clearInterval(iv);
        }
      }, 2000);
    },

    async approve(c) {
      const r = await fetch(`/clips/${c.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ status: "approved" }),
      });
      if (r.ok) c.status = "approved";
    },

    async del(c) {
      const r = await fetch(`/clips/${c.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ status: "deleted" }),
      });
      if (r.ok) this.clips = this.clips.filter((x) => x.id !== c.id);
    },

    // helpers
    async postJSON(url, body) {
      const r = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!r.ok) {
        const j = await r.json().catch(() => ({}));
        throw new Error(j.error?.message || `HTTP ${r.status}`);
      }
      return r.json();
    },
    platformLabel(key) {
      return this.platforms.find((p) => p.key === key)?.label || key;
    },
    stageLabel(stage) {
      const m = {
        queued: "Queued", transcribing: "Transcribing",
        analyzing: "Analysing scenes & audio", finding_highlights: "Finding highlights",
        cutting: "Cutting", reframing: "Reframing",
        rendering: "Rendering", finalizing: "Finalising", done: "Done",
      };
      return m[stage] || stage || "";
    },
    fmtDur(s) {
      if (!s) return "";
      const m = Math.floor(s / 60), sec = Math.round(s % 60);
      return `${m}m ${sec}s`;
    },
  };
}
