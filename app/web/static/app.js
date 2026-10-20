function clipforge() {
  return {
    steps: ["Upload", "Rights", "Settings", "Processing", "Review"],
    step: "upload",
    source: "file", // "file" | "link"
    link: { url: "", checking: false, importing: false, info: null, quality: "best" },
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
    notice: "",
    // Background music library + the review screen's "all clips" controls.
    tracks: [],
    music: { trackId: "", volume: 40, muteOriginal: false, uploading: false, busy: false, link: "", grabbing: false },
    // Colour grade applied to all clips at once (each clip also has its own).
    colorAll: { contrast: 0, brightness: 0, saturation: 0, busy: false },
    _trackPoll: null,
    _poll: null,
    _watch: null,

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
      await this.loadTracks();
    },

    reset() {
      if (this._poll) clearInterval(this._poll);
      if (this._watch) clearInterval(this._watch);
      this._watch = null;
      this.music.busy = false;
      Object.assign(this, {
        step: "upload", file: null, uploading: false, uploadPct: 0,
        link: { url: "", checking: false, importing: false, info: null, quality: "best" },
        colorAll: { contrast: 0, brightness: 0, saturation: 0, busy: false },
        video: null, rightsChecked: false, platformCfg: {},
        captions: { enabled: true, language: "" },
        job: null, clips: [], busy: false, error: "", notice: "",
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

    // Import from a link: check it (lists qualities), then download in the background.
    async checkLink() {
      if (!this.link.url || this.link.checking) return;
      this.link.checking = true;
      this.link.info = null;
      try {
        this.link.info = await this.postJSON("/videos/from-url/qualities", { url: this.link.url });
        this.link.quality = "best";
      } catch (e) {
        this.error = e.message;
      } finally {
        this.link.checking = false;
      }
    },

    async importLink() {
      if (!this.link.info || this.link.importing) return;
      this.link.importing = true;
      try {
        this.video = await this.postJSON("/videos/from-url", {
          url: this.link.url,
          quality: this.link.quality,
        });
        this.step = "rights";
        this.pollVideo();
      } catch (e) {
        this.error = e.message;
      } finally {
        this.link.importing = false;
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
        if (!cur) {
          this.initMusic(f);
          this.initColor(f);
          return f;
        }
        Object.assign(cur, {
          status: f.status,
          has_file: f.has_file,
          file_version: f.file_version,
          has_thumbnail: f.has_thumbnail,
          has_srt: f.has_srt,
          error: f.error,
          bg_audio_id: f.bg_audio_id,
          bg_audio_volume: f.bg_audio_volume,
          bg_audio_mute_original: f.bg_audio_mute_original,
        });
        return cur;
      });
    },

    // A clip's player is only created once its file is ready. file_version (the
    // file's mtime, from the server) changes whenever the file is rewritten -
    // re-render, music added/removed - so the URL changes and the browser can
    // never play a stale cached copy (a per-page counter restarts at 0 after a
    // reload and hit old cache entries).
    isPlayable(c) {
      return c.has_file && ["ready", "approved"].includes(c.status);
    },

    previewUrl(c) {
      return `/clips/${c.id}/preview?v=${c.file_version || 0}`;
    },

    downloadUrl(c) {
      return `/clips/${c.id}/download?v=${c.file_version || 0}`;
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
        color_contrast: c.color_contrast ?? 0,
        color_brightness: c.color_brightness ?? 0,
        color_saturation: c.color_saturation ?? 0,
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
        this.initColor(c); // current grade is now what's being rendered
        this.watchClip(c);
      }
    },

    // ----- Colour grade (contrast / brightness / saturation) -----
    initColor(c) {
      c._colorApplied = {
        c: c.color_contrast ?? 0,
        b: c.color_brightness ?? 0,
        s: c.color_saturation ?? 0,
      };
    },

    colorChanged(c) {
      const a = c._colorApplied || { c: 0, b: 0, s: 0 };
      return (
        (c.color_contrast ?? 0) !== a.c ||
        (c.color_brightness ?? 0) !== a.b ||
        (c.color_saturation ?? 0) !== a.s
      );
    },

    resetColor(c) {
      c.color_contrast = 0;
      c.color_brightness = 0;
      c.color_saturation = 0;
    },

    // Apply one grade to every ready clip; each re-renders (keeps its music).
    async applyColorAll() {
      this.colorAll.busy = true;
      try {
        const res = await this.postJSON(`/jobs/${this.job.id}/color`, {
          contrast: this.colorAll.contrast,
          brightness: this.colorAll.brightness,
          saturation: this.colorAll.saturation,
        });
        const byId = new Map(this.clips.map((c) => [c.id, c]));
        for (const f of res.clips) {
          const c = byId.get(f.id);
          if (!c) continue;
          Object.assign(c, {
            status: f.status,
            error: f.error,
            color_contrast: f.color_contrast,
            color_brightness: f.color_brightness,
            color_saturation: f.color_saturation,
          });
          this.initColor(c);
        }
        if (res.skipped) {
          this.notice = `Re-rendering ${res.queued} clip(s) with the new colour. ${res.skipped} not ready were skipped.`;
        }
        this.watchRendering();
      } catch (e) {
        this.error = e.message;
      } finally {
        this.colorAll.busy = false;
      }
    },

    // Clear the grade on every clip that has one (re-renders those clips).
    resetColorAll() {
      this.colorAll.contrast = 0;
      this.colorAll.brightness = 0;
      this.colorAll.saturation = 0;
      return this.applyColorAll();
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

    // ----- Background music -----
    async loadTracks() {
      try {
        const r = await fetch("/audio");
        if (r.ok) this.tracks = await r.json();
      } catch (e) {}
      const ready = this.readyTracks();
      if (!ready.some((t) => t.id === this.music.trackId)) {
        this.music.trackId = ready[0]?.id || "";
      }
      // Keep polling while a link import is downloading.
      const busy = this.tracks.some((t) => t.status === "processing");
      if (busy && !this._trackPoll) {
        this._trackPoll = setInterval(() => this.loadTracks(), 1500);
      } else if (!busy && this._trackPoll) {
        clearInterval(this._trackPoll);
        this._trackPoll = null;
      }
    },

    readyTracks() {
      return this.tracks.filter((t) => t.status === "ready");
    },

    // Grab only the audio of a link (YouTube, SoundCloud, ...). Downloads in
    // the background; the track shows its progress until it is ready.
    async grabMusicLink() {
      if (!this.music.link || this.music.grabbing) return;
      this.music.grabbing = true;
      try {
        const t = await this.postJSON("/audio/from-url", { url: this.music.link });
        this.music.link = "";
        this.tracks = [t, ...this.tracks];
        await this.loadTracks();
      } catch (e) {
        this.error = e.message;
      } finally {
        this.music.grabbing = false;
      }
    },

    async removeTrack(t) {
      const r = await fetch(`/audio/${t.id}`, { method: "DELETE" });
      if (!r.ok) {
        this.error = (await r.json().catch(() => ({}))).error?.message || "Delete failed";
      }
      await this.loadTracks();
    },

    async uploadMusic(e) {
      const f = e.target.files[0];
      e.target.value = "";
      if (!f) return;
      this.music.uploading = true;
      try {
        const fd = new FormData();
        fd.append("file", f);
        const r = await fetch("/audio/upload", { method: "POST", body: fd });
        const j = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(j.error?.message || `HTTP ${r.status}`);
        await this.loadTracks();
        this.music.trackId = j.id;
        this.notice = `"${j.filename}" added. Pick it and click "Add to all clips", or tick "Background music" on a clip.`;
      } catch (err) {
        this.error = "Audio upload failed: " + err.message;
      } finally {
        this.music.uploading = false;
      }
    },

    async deleteTrack() {
      const t = this.tracks.find((x) => x.id === this.music.trackId);
      if (!t || !confirm(`Delete "${t.filename}" from your music?`)) return;
      const r = await fetch(`/audio/${t.id}`, { method: "DELETE" });
      if (!r.ok) {
        this.error = (await r.json().catch(() => ({}))).error?.message || "Delete failed";
        return;
      }
      await this.loadTracks();
    },

    // One endpoint for both: clipIds = [id] for one clip, null for every ready
    // clip. audioId null removes the music.
    async setMusic(audioId, volume, clipIds, muteOriginal = false) {
      this.music.busy = true;
      try {
        const res = await this.postJSON(`/jobs/${this.job.id}/background-audio`, {
          audio_id: audioId,
          volume: volume,
          mute_original: muteOriginal,
          clip_ids: clipIds,
        });
        const byId = new Map(this.clips.map((c) => [c.id, c]));
        for (const f of res.clips) {
          const c = byId.get(f.id);
          if (!c) continue;
          Object.assign(c, {
            status: f.status,
            error: f.error,
            bg_audio_id: f.bg_audio_id,
            bg_audio_volume: f.bg_audio_volume,
            bg_audio_mute_original: f.bg_audio_mute_original,
          });
          this.initMusic(c);
        }
        if (audioId && !clipIds && res.skipped) {
          this.notice = `Adding music to ${res.queued} clip(s). ${res.skipped} not ready yet were skipped: add it to them when they finish.`;
        }
        this.watchRendering();
      } catch (e) {
        this.error = e.message;
      } finally {
        this.music.busy = false;
      }
    },

    // Per-clip editor state starts from what the server has; a clip without
    // music starts from the panel's track and volume.
    initMusic(c) {
      c._musicId = c.bg_audio_id || this.music.trackId || "";
      c._musicVol = c.bg_audio_id ? c.bg_audio_volume : this.music.volume;
      c._musicMute = c.bg_audio_id ? c.bg_audio_mute_original : this.music.muteOriginal;
    },

    // The clip's "Background music" checkbox: tick = add, untick = remove.
    toggleClipMusic(c, on) {
      if (!on) return this.setMusic(null, c._musicVol, [c.id]);
      const id = c._musicId || this.music.trackId;
      if (!id) {
        this.error = "Upload a track or grab one from a link first.";
        return;
      }
      return this.setMusic(id, c._musicVol, [c.id], c._musicMute);
    },

    // Track, volume or mute changed on a clip that already has music.
    musicChanged(c) {
      if (!c.bg_audio_id) return false;
      return (
        c._musicId !== c.bg_audio_id ||
        c._musicVol !== c.bg_audio_volume ||
        c._musicMute !== c.bg_audio_mute_original
      );
    },

    // Poll until no clip is rendering/mixing; players reload as each finishes.
    watchRendering() {
      if (this._watch) return;
      this._watch = setInterval(async () => {
        try {
          await this.refreshClips();
        } catch (e) {}
        if (!this.clips.some((c) => c.status === "rendering")) {
          clearInterval(this._watch);
          this._watch = null;
        }
      }, 1500);
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
    fmtSize(b) {
      if (!b) return "";
      return b >= 1e9 ? `${(b / 1e9).toFixed(1)} GB` : `${Math.round(b / 1e6)} MB`;
    },
    fmtDur(s) {
      if (!s) return "";
      const m = Math.floor(s / 60), sec = Math.round(s % 60);
      return `${m}m ${sec}s`;
    },
  };
}
