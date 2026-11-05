const NOTICE_MS = 3000; // how long a notice (toast) stays

// Frame (services/frame.py): a clip made 1080x1920 with the picture in the
// middle and text above/below it. These match the server's numbers.
const FRAME_W = 1080;
const FRAME_H = 1920;
const FRAME_TEXT_W = 960; // a text line wraps when wider (frame.TEXT_WRAP_W)
// Emoji are not drawn in the video (libass can't), so the preview drops them too.
const FRAME_EMOJI = /[\u{1F000}-\u{1FAFF}\u{2600}-\u{27BF}\u{2B00}-\u{2BFF}\u{FE0F}\u{200D}]/gu;
// The mobile preview's <video>/<canvas> per clip id. Kept outside Alpine: a
// reactive proxy of a <video> can't be drawn on a canvas.
const MOBILE_EL = new Map();

function clipforge() {
  return {
    steps: ["Upload", "Rights", "Trim", "Settings", "Processing", "Review"],
    step: "upload",
    // "clip" (upload -> clips) | "reel" (Create a reel, reels.js) | "bucket" (bucket.js)
    // | "local" (Local videos: what is kept on this computer, local.js)
    mode: "clip",
    reelSource: "topic", // Create a reel: "topic" (reels.js) | "video" (trim a video, reel_video.js)
    source: "file", // "file" | "link"
    link: { url: "", checking: false, importing: false, info: null, quality: "best" },
    file: null,
    uploading: false,
    uploadPct: 0,
    video: null,
    rightsChecked: false,
    // Trim step: the stretch of the video cut into clips (seconds); the whole
    // video unless the user picks a part.
    range: { start: 0, end: 0 },
    rangePlayerError: false, // the browser can't play this file: times are typed instead
    playhead: 0, // where the player is (the white line on the trim bar)
    stripOk: true, // the frames behind the trim bar loaded
    _rangeStopAt: null, // a "Play" button pauses here
    _barDrag: null, // being dragged on the trim bar: "start" | "end" | "playhead"
    platforms: [],
    // { platform_key: { lengthS } } - seconds per clip for each chosen platform
    platformCfg: {},
    captions: { enabled: true, language: "" },
    transcribe: true, // Settings step: speech-to-text on/off
    job: null,
    clips: [],
    // Mobile preview (review step): clip ids shown in phone frames; empty = closed.
    // playing / progress / more are per clip id (paused?, 0-100 %, caption expanded?).
    // draft: the frame being edited (null = none); sel: its selected text layer; guide: show the centre line.
    _mobileDrafts: {}, // clip id -> an edit not applied when the preview was closed
    mobile: { ids: [], playing: {}, progress: {}, more: {}, draft: null, sel: -1, guide: false, showUi: true, applying: false, srcError: {}, dims: {} },
    busy: false,
    error: "",
    notice: "",
    _noticeTimer: null, // hides the notice after NOTICE_MS
    // Background music library + the review screen's "all clips" controls.
    tracks: [],
    music: { trackId: "", volume: 40, muteOriginal: false, uploading: false, busy: false, link: "", grabbing: false },
    // Audio files in the bucket (if one is set up): the pickers offer them too.
    bucketMusic: [],
    fetchingMusic: false, // a bucket file is being brought into the library
    wiping: false, // "Delete all data" running
    // Colour grade applied to all clips at once (each clip also has its own).
    colorAll: { contrast: 0, brightness: 0, saturation: 0, busy: false },
    _trackPoll: null,
    _poll: null,
    _watch: null,
    _upload: null, // AbortController of the upload in progress
    _uploadDone: null, // settles when that upload loop has stopped
    _uploadVideoId: null,

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
      this.loadBucketMusic();
      // Captions come from the transcript: off with it, back on with it.
      this.$watch("transcribe", (on) => (this.captions.enabled = on));
      this.$watch("notice", (msg) => (msg ? this.releaseNotice() : this.holdNotice()));
    },

    // A notice goes away by itself; the mouse on it keeps it (long messages).
    // Errors stay until closed, so they aren't missed.
    holdNotice() {
      clearTimeout(this._noticeTimer);
      this._noticeTimer = null;
    },
    releaseNotice() {
      this.holdNotice();
      if (this.notice) this._noticeTimer = setTimeout(() => (this.notice = ""), NOTICE_MS);
    },

    // Start over. Work still running is stopped and what it made is deleted,
    // after a confirm: an unfinished upload or download loses its partial
    // video; a queued/running job loses its clips, but the video and its
    // transcript stay so processing it again is quick. Finished work is kept.
    async reset() {
      const running = this.runningWork();
      if (running && !confirm(running.question)) return;
      if (this._poll) clearInterval(this._poll);
      if (this._watch) clearInterval(this._watch);
      this._watch = null;
      this.music.busy = false;
      if (running) {
        try {
          await running.stop();
        } catch (e) {} // already finished or gone: nothing left to stop
      }
      Object.assign(this, {
        step: "upload", file: null, uploading: false, uploadPct: 0,
        link: { url: "", checking: false, importing: false, info: null, quality: "best" },
        colorAll: { contrast: 0, brightness: 0, saturation: 0, busy: false },
        video: null, rightsChecked: false, platformCfg: {},
        range: { start: 0, end: 0 }, rangePlayerError: false, playhead: 0, stripOk: true,
        _rangeStopAt: null, _barDrag: null,
        captions: { enabled: true, language: "" }, transcribe: true,
        job: null, clips: [], busy: false, error: "", notice: "",
      });
    },

    // Free disk space: delete every video, job, clip and transcript with
    // their files, after typing DELETE. The music library and the Whisper
    // model are kept. The server refuses while work is still running.
    async deleteAllData() {
      if (this.wiping) return;
      try {
        const r = await fetch("/data/usage");
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const u = await r.json();
        const typed = prompt(
          "Delete ALL data to free disk space?\n\n" +
            `This permanently deletes every video (${u.videos}), job (${u.jobs}) and ` +
            `clip (${u.clips}) with their transcripts: ${this.fmtSize(u.bytes) || "0 MB"}.\n` +
            "Your music library and the Whisper model are kept.\n\n" +
            "Download your ZIP first. Type DELETE to confirm:"
        );
        if (typed === null) return;
        if (typed.trim() !== "DELETE") {
          this.error = "Nothing deleted: type DELETE to confirm";
          return;
        }
        this.wiping = true;
        const res = await this.postJSON("/data/delete-all", { confirm: "DELETE" });
        await this.reset();
        this.notice =
          `Deleted ${res.videos} videos and ${res.clips} clips, ` +
          `freed ${this.fmtSize(res.bytes_freed) || "0 MB"}`;
      } catch (e) {
        this.error = e.message;
      } finally {
        this.wiping = false;
      }
    },

    runningWork() {
      if (this.uploading && this._upload) {
        const ctrl = this._upload;
        return {
          question: "The upload isn't finished.\n\nStop it and delete the partial video?",
          stop: async () => {
            ctrl.abort();
            await this._uploadDone;
            if (this._uploadVideoId) {
              await fetch(`/videos/${this._uploadVideoId}/abort`, { method: "POST" });
            }
          },
        };
      }
      if (this.video?.status === "downloading") {
        const id = this.video.id;
        return {
          question: "The video is still downloading.\n\nStop it and delete the partial download?",
          stop: () => fetch(`/videos/${id}/abort`, { method: "POST" }),
        };
      }
      if (this.job && ["queued", "running"].includes(this.job.status)) {
        const id = this.job.id;
        return {
          question:
            "Processing is still running.\n\nStop it and delete this job's clips? " +
            "The video and its transcript are kept, so processing it again is quick.",
          stop: () => fetch(`/jobs/${id}/cancel`, { method: "POST" }),
        };
      }
      return null;
    },

    pickFile(e) {
      this.file = e.target.files[0] || null;
    },

    async startUpload() {
      if (!this.file) return;
      const ctrl = new AbortController(); // Start over aborts it
      let settle;
      this._upload = ctrl;
      this._uploadDone = new Promise((resolve) => (settle = resolve));
      this._uploadVideoId = null;
      this.uploading = true;
      this.uploadPct = 0;
      try {
        const init = await this.postJSON("/videos/upload/init", {
          filename: this.file.name,
          size_bytes: this.file.size,
        });
        const vid = init.video_id;
        this._uploadVideoId = vid;
        const chunk = init.chunk_size;
        let offset = 0;
        while (offset < this.file.size) {
          if (ctrl.signal.aborted) return;
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
            signal: ctrl.signal,
          });
          offset += slice.size;
          this.uploadPct = Math.round((offset / this.file.size) * 100);
        }
        if (ctrl.signal.aborted) return;
        this.video = await this.postJSON(`/videos/${vid}/upload/complete`, {});
        this.uploading = false;
        this.step = "rights";
        this.pollVideo();
      } catch (e) {
        if (ctrl.signal.aborted) return; // stopped on purpose (Start over)
        this.uploading = false;
        this.error = "Upload failed: " + e.message;
      } finally {
        if (this._upload === ctrl) this._upload = null;
        settle();
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
        this.wholeVideo();
        Object.assign(this, { playhead: 0, stripOk: true });
        this.step = "trim";
      } catch (e) {
        this.error = e.message;
      }
    },

    // ----- Trim step: the stretch of the video to cut into clips -----
    wholeVideo() {
      this.range = { start: 0, end: this.video?.duration_s || 0 };
    },
    isTrimmed() {
      const dur = this.video?.duration_s || 0;
      return this.range.start > 0.05 || this.range.end < dur - 0.05;
    },
    rangeLen() {
      return Math.max(0, this.range.end - this.range.start);
    },
    // Why Continue is disabled ("" = it isn't). Each platform's own shortest
    // clip is checked by the server once platforms are picked.
    rangeProblem() {
      const dur = this.video?.duration_s;
      if (!(this.range.end > this.range.start)) return "The end must be after the start.";
      if (this.range.start < 0 || (dur && this.range.end > dur + 0.5)) return "This part is outside the video.";
      const shortest = Math.min(...this.platforms.map((p) => p.length_min_s));
      if (this.rangeLen() < shortest) return `Pick at least ${shortest} s.`;
      return "";
    },
    markRange(which) {
      const v = this.$refs.trimPlayer;
      if (v) this.range[which] = Math.round(v.currentTime * 10) / 10;
    },
    // A typed time ("1:23.5", "83.5" or "1:02:03"); the player jumps there.
    setRange(which, e) {
      const t = this.parseTime(e.target.value);
      if (Number.isNaN(t)) {
        this.error = "Type a time like 1:23.5 (minutes:seconds) or 83.5 (seconds).";
      } else {
        this.range[which] = Math.round(t * 10) / 10;
        const v = this.$refs.trimPlayer;
        if (v && !this.rangePlayerError) v.currentTime = this.range[which];
      }
      e.target.value = this.fmtTime(this.range[which]);
    },
    // Play from the start of the part, or its last 5 seconds (to check the end).
    playRange(which) {
      const v = this.$refs.trimPlayer;
      if (!v) return;
      v.currentTime = which === "end" ? Math.max(this.range.start, this.range.end - 5) : this.range.start;
      this._rangeStopAt = this.range.end;
      v.play().catch(() => {});
    },
    onRangeTime() {
      const v = this.$refs.trimPlayer;
      if (!v) return;
      if (!this._barDrag) this.playhead = v.currentTime;
      if (this._rangeStopAt !== null && v.currentTime >= this._rangeStopAt) {
        v.pause();
        this._rangeStopAt = null;
      }
    },
    // While playing, the white line follows smoothly (timeupdate comes ~4 times a second).
    followPlayer() {
      const v = this.$refs.trimPlayer;
      if (!v || v.paused) return;
      this.onRangeTime();
      requestAnimationFrame(() => this.followPlayer());
    },

    // ----- The trim bar: drag a handle to move the start or end; press or
    // drag anywhere else to move the player there -----
    pct(t) {
      return Math.max(0, Math.min(100, (t / (this.video?.duration_s || 1)) * 100));
    },
    labelPct(t) {
      return Math.max(5, Math.min(95, this.pct(t))); // keeps the time labels on screen
    },
    timeAt(e) {
      const bar = this.$refs.trimBar.getBoundingClientRect();
      const x = Math.min(Math.max(e.clientX - bar.left, 0), bar.width);
      return Math.round((x / bar.width) * (this.video?.duration_s || 0) * 10) / 10;
    },
    grabBar(which, e) {
      this._barDrag = which;
      if (which !== "playhead") this.$refs.trimPlayer?.pause(); // show the frame at the handle
      this.dragBar(e);
    },
    dragBar(e) {
      const which = this._barDrag;
      if (!which) return;
      const t = this.timeAt(e);
      if (which === "start") this.range.start = Math.min(t, Math.max(0, this.range.end - 1));
      if (which === "end") this.range.end = Math.max(t, this.range.start + 1);
      this.seekTo(which === "playhead" ? t : this.range[which]);
    },
    dropBar() {
      this._barDrag = null;
    },
    // A focused handle and ← →: 0.1 s (Shift: 1 s). The handles stay 1 s apart.
    nudge(which, delta) {
      const dur = this.video?.duration_s || 0;
      const t = Math.round((this.range[which] + delta) * 10) / 10;
      this.range[which] = which === "start"
        ? Math.max(0, Math.min(t, this.range.end - 1))
        : Math.min(dur, Math.max(t, this.range.start + 1));
      this.seekTo(this.range[which]);
    },
    seekTo(t) {
      this.playhead = t;
      const v = this.$refs.trimPlayer;
      if (v && !this.rangePlayerError) v.currentTime = t;
    },

    togglePlatform(p) {
      if (p.key in this.platformCfg) {
        delete this.platformCfg[p.key];
      } else {
        // A sensible per-format length: 40 s vertical, 3 min horizontal.
        const want = p.is_vertical ? 40 : 180;
        const lengthS = Math.min(p.length_max_s, Math.max(p.length_min_s, want));
        this.platformCfg[p.key] = { lengthS };
      }
    },

    coverageEstimate(p) {
      const cfg = this.platformCfg[p.key];
      const dur = this.isTrimmed() ? this.rangeLen() : this.video?.duration_s;
      if (!cfg || !dur || !cfg.lengthS) return "";
      const n = Math.max(1, Math.ceil(dur / cfg.lengthS));
      const what = this.isTrimmed() ? "the part you picked" : "the whole video";
      return `≈ ${n} clip${n === 1 ? "" : "s"} to cover ${what}`;
    },

    async createJob() {
      this.busy = true;
      try {
        const platforms = Object.keys(this.platformCfg);
        const coverage_lengths = {};
        for (const k of platforms) coverage_lengths[k] = this.platformCfg[k].lengthS;
        const body = {
          platforms: platforms,
          coverage_lengths: coverage_lengths,
          transcribe: this.transcribe,
          captions: {
            enabled: this.transcribe && this.captions.enabled,
            language: this.captions.language || null,
          },
        };
        if (this.isTrimmed()) {
          body.source_range = { start_s: this.range.start, end_s: Math.min(this.range.end, this.video.duration_s) };
        }
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
        // A re-render just finished: take the grade it really gave the file.
        const finished = cur.status === "rendering" && ["ready", "approved"].includes(f.status);
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
          color_rendered: f.color_rendered,
          frame: f.frame,
          frame_rendered: f.frame_rendered,
          bucket: f.bucket,
        });
        if (finished) {
          Object.assign(cur, {
            color_contrast: f.color_contrast,
            color_brightness: f.color_brightness,
            color_saturation: f.color_saturation,
          });
          this.initColor(cur);
        }
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

    // Mobile preview: shows a clip (or every ready clip) inside a phone frame
    // laid out like the Facebook app, to see how it will look on a phone.
    // Preview only - nothing is saved or re-rendered.
    openMobile(c = null) {
      const ids = (c ? [c] : this.clips).filter((x) => this.isPlayable(x)).map((x) => x.id);
      if (!ids.length) {
        this.notice = "No ready clips to preview yet.";
        return;
      }
      // The editor starts from an edit left unapplied for this clip (closed by
      // mistake), else from the frame the (first) clip has saved.
      const first = this.clips.find((x) => x.id === ids[0]);
      const kept = this._mobileDrafts[first.id];
      const saved = kept !== undefined ? kept : first.frame;
      const draft = saved ? JSON.parse(JSON.stringify(saved)) : null;
      const sel = draft?.texts?.length ? 0 : -1;
      this.mobile = { ids, playing: {}, progress: {}, more: {}, draft, sel, guide: false, showUi: true, applying: false, srcError: {}, dims: {} };
    },
    closeMobile(e = null) {
      if (e && /INPUT|TEXTAREA|SELECT/.test(e.target?.tagName)) return; // Esc while typing
      const first = this.mobileClips()[0];
      if (first && !this.frameApplied(first)) {
        this._mobileDrafts[first.id] = this.mobile.draft ? JSON.parse(JSON.stringify(this.mobile.draft)) : null;
      } else if (first) {
        delete this._mobileDrafts[first.id];
      }
      this.mobile.ids = [];
      MOBILE_EL.clear();
    },
    mobileClips() {
      return this.mobile.ids.map((id) => this.clips.find((c) => c.id === id)).filter(Boolean);
    },
    // 9:16 clips fill the phone screen; 16:9 ones sit in the middle with black above and below.
    isVertical(c) {
      return this.platforms.find((p) => p.key === c.platform)?.aspect === "9:16";
    },
    // The post text under the clip, as Facebook shows it: title, description, hashtags.
    mobileCaption(c) {
      const tags = (c.hashtags || []).map((h) => "#" + h).join(" ");
      return [c.title, c.description, tags].map((s) => (s || "").trim()).filter(Boolean).join(" ");
    },
    toggleMobilePlay(el) {
      el.paused ? el.play() : el.pause();
    },

    // ----- Frame editor (mobile preview), like a video editing app: the picture
    // placed and sized on a 1080x1920 canvas, any number of text layers dragged
    // anywhere on it (also on the picture). Same layout as services/frame.py.
    newText(over = {}) {
      return {
        text: "New text", x: 540, y: 960, align: "center", size: 64, color: "#ffffff",
        bold: true, italic: false, outline: true, box: false, box_color: "#000000", ...over,
      };
    },
    defaultFrame() {
      return {
        background: "blur", bg_color: "#000000", video_x: 50, video_y: 50, video_scale: 100,
        texts: [
          this.newText({ text: "Your title here", y: 330 }),
          this.newText({ text: "Part {part}", y: 1450, size: 56, box: true, box_color: "#c00000", outline: false }),
        ],
      };
    },
    toggleFrame(on) {
      this.mobile.draft = on ? this.defaultFrame() : null;
      this.mobile.sel = on ? 0 : -1;
    },
    selText() {
      return this.mobile.draft?.texts?.[this.mobile.sel] || null;
    },
    addText() {
      const texts = this.mobile.draft.texts;
      texts.push(this.newText({ y: Math.min(1800, 960 + (texts.length % 5) * 90) }));
      this.mobile.sel = texts.length - 1;
    },
    copyText(i) {
      const texts = this.mobile.draft.texts;
      const t = JSON.parse(JSON.stringify(texts[i]));
      texts.splice(i + 1, 0, { ...t, y: Math.min(1920, t.y + 90) });
      this.mobile.sel = i + 1;
    },
    delText(i) {
      this.mobile.draft.texts.splice(i, 1);
      this.mobile.sel = Math.min(this.mobile.sel, this.mobile.draft.texts.length - 1);
    },
    // Later layers are drawn on top: "up" brings a text in front of the next one.
    moveLayer(i, d) {
      const texts = this.mobile.draft.texts;
      const j = i + d;
      if (j < 0 || j >= texts.length) return;
      [texts[i], texts[j]] = [texts[j], texts[i]];
      this.mobile.sel = j;
    },
    // Arrow keys nudge the selected text (Shift: 10x), unless typing in a field.
    frameNudge(e) {
      const t = this.selText();
      const keys = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] };
      if (!t || !keys[e.key] || /INPUT|TEXTAREA|SELECT/.test(e.target.tagName)) return;
      e.preventDefault();
      const step = e.shiftKey ? 20 : 2;
      t.x = Math.min(FRAME_W, Math.max(0, t.x + keys[e.key][0] * step));
      t.y = Math.min(FRAME_H, Math.max(0, t.y + keys[e.key][1] * step));
    },
    // Same layout as frame.picture_rect: [x, y, w, h] of the picture on the canvas.
    frameLayout(sw, sh, f) {
      const even = (v) => Math.max(2, Math.round(v / 2) * 2);
      const fit = (Math.min(FRAME_W / sw, FRAME_H / sh) * (f.video_scale ?? 100)) / 100;
      const w = Math.min(even(sw * fit), FRAME_W);
      const h = Math.min(even(sh * fit), FRAME_H);
      const x = Math.round((FRAME_W * (f.video_x ?? 50)) / 100 - w / 2);
      const y = Math.round((FRAME_H * (f.video_y ?? 50)) / 100 - h / 2);
      const keep = (v, room) => Math.floor(Math.min(Math.max(v, 0), room) / 2) * 2;
      return [keep(x, FRAME_W - w), keep(y, FRAME_H - h), w, h];
    },
    // Drag on the phone (1080x1920 box): a text moves that text, the picture
    // moves the picture; a tap without moving plays / pauses.
    framePointer(e, c, i = null) {
      const box = e.currentTarget.closest("[data-frame-box]");
      if (!box || e.button > 0) return;
      e.preventDefault();
      const f = this.mobile.draft;
      const scale = FRAME_H / box.clientHeight; // canvas px per screen px
      const r = box.getBoundingClientRect();
      const [px, py, pw, ph] = this.frameLayout(...this.mobilePicSize(c), f);
      let kind = "none";
      let orig = null;
      if (i != null) {
        kind = "text";
        this.mobile.sel = i;
        orig = { x: f.texts[i].x, y: f.texts[i].y };
      } else {
        const cx = (e.clientX - r.left) * scale;
        const cy = (e.clientY - r.top) * scale;
        if (cx >= px && cx <= px + pw && cy >= py && cy <= py + ph) {
          kind = "video";
          orig = { x: f.video_x ?? 50, y: f.video_y ?? 50 };
        }
      }
      const start = { x: e.clientX, y: e.clientY };
      let moved = false;
      const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
      const move = (ev) => {
        const dx = (ev.clientX - start.x) * scale;
        const dy = (ev.clientY - start.y) * scale;
        if (!moved && Math.hypot(dx, dy) < 8) return;
        moved = true;
        if (kind === "text") {
          const t = f.texts[i];
          let x = clamp(orig.x + dx, 0, FRAME_W);
          this.mobile.guide = Math.abs(x - FRAME_W / 2) < 16; // snap to the centre line
          if (this.mobile.guide) x = FRAME_W / 2;
          t.x = Math.round(x);
          t.y = Math.round(clamp(orig.y + dy, 0, FRAME_H));
        } else if (kind === "video") {
          // Only the room the picture has: a full-width picture moves up / down only.
          if (pw < FRAME_W) {
            let vx = clamp(orig.x + (dx / FRAME_W) * 100, 0, 100);
            this.mobile.guide = Math.abs(vx - 50) < 1.5;
            if (this.mobile.guide) vx = 50;
            f.video_x = Math.round(vx * 10) / 10;
          }
          if (ph < FRAME_H) f.video_y = Math.round(clamp(orig.y + (dy / FRAME_H) * 100, 0, 100) * 10) / 10;
        }
      };
      const up = () => {
        window.removeEventListener("pointermove", move);
        this.mobile.guide = false;
        if (!moved && kind !== "text") this.mobileToggle(c);
      };
      window.addEventListener("pointermove", move);
      window.addEventListener("pointerup", up, { once: true });
    },
    // Where the preview takes the picture from:
    //  - the file has a frame: the picture's part of it (frame_rendered.rect)
    //  - a framed 9:16 clip not made yet: its file is a crop, but a frame uses
    //    the whole picture, so that part of the source video is played
    //  - otherwise the whole file
    mobileSource(c) {
      if (c.frame_rendered?.rect) return { url: this.previewUrl(c), rect: c.frame_rendered.rect };
      if (this.mobile.draft && this.isVertical(c) && this.job?.video_id) {
        return { url: `/videos/${this.job.video_id}/source`, rect: null, from: c.start_s, to: c.end_s };
      }
      return { url: this.previewUrl(c), rect: null };
    },
    // The picture's size [w, h] in the preview: what the frame is laid out from.
    mobilePicSize(c) {
      const src = this.mobileSource(c);
      if (src.rect) return [src.rect[2], src.rect[3]];
      if (this.mobile.dims[c.id]) return this.mobile.dims[c.id];
      return this.isVertical(c) && !src.from ? [1080, 1920] : [1920, 1080];
    },
    // Without a frame: shown like Facebook shows the file. A landscape picture
    // (or the picture cut out of a framed file) sits full-width a little above
    // the middle; a 9:16 clip fills the screen.
    mobileLandscape(c) {
      return !this.mobile.draft && (!!c.frame_rendered?.rect || !this.isVertical(c));
    },
    // The box the <video> shows in (cqw = 1% of the phone's width).
    mobileBoxStyle(c) {
      if (this.mobile.draft) return "inset: 0; opacity: 0;"; // drawn on the canvas instead
      if (!this.mobileLandscape(c)) return "inset: 0;";
      const r = c.frame_rendered?.rect;
      const ratio = r ? r[3] / r[2] : 9 / 16;
      return `left: 0; right: 0; aspect-ratio: ${r ? `${r[2]} / ${r[3]}` : "16 / 9"}; top: calc(44% - ${ratio * 50}cqw);`;
    },
    // A framed file: the video is enlarged and moved so only its picture part fills the box.
    mobileVideoStyle(c) {
      const r = c.frame_rendered?.rect;
      const grade = this.colorChanged(c) ? `filter: url(#cf-${c.id});` : "";
      if (this.mobile.draft || !r) return grade;
      return `${grade} width: ${(FRAME_W / r[2]) * 100}%; height: auto; max-width: none; left: ${(-r[0] / r[2]) * 100}%; top: ${(-r[1] / r[3]) * 100}%;`;
    },
    mobileVideoClass(c) {
      if (this.mobile.draft || c.frame_rendered?.rect) return "";
      return this.isVertical(c) ? "inset-0 w-full h-full object-cover" : "inset-0 w-full h-full object-contain";
    },
    mobileToggle(c) {
      const v = MOBILE_EL.get(c.id)?.video;
      if (v) this.toggleMobilePlay(v);
    },
    // A text layer's words for one clip: placeholders filled, emoji dropped.
    frameText(c, t) {
      if (!t?.text) return "";
      return t.text.replaceAll("{part}", c.rank ?? "").replaceAll("{title}", c.title || "").replace(FRAME_EMOJI, "").trim();
    },
    // CSS for a text layer in the 1080x1920 box (container-type: size, so
    // 100cqh = 1920 canvas px), matching frame._style / build_ass.
    frameTextStyle(t, i) {
      const u = (px) => `calc(${px} * 100cqh / ${FRAME_H})`;
      const shift = { left: "0%", center: "-50%", right: "-100%" }[t.align] || "-50%";
      const edge = Math.max(2, Math.round(t.size * 0.06));
      const outline = t.outline && !t.box ? `-webkit-text-stroke: ${u(edge * 2)} rgba(0,0,0,.75); paint-order: stroke fill;` : "";
      const picked = this.mobile.sel === i ? "outline: 2px dashed #a5b4fc; outline-offset: 3px;" : "";
      return `left: ${(t.x / FRAME_W) * 100}%; top: ${(t.y / FRAME_H) * 100}%; transform: translate(${shift}, -50%);` +
        `width: max-content; max-width: ${(FRAME_TEXT_W / FRAME_W) * 100}%; text-align: ${t.align};` +
        `font-size: ${u(t.size)}; line-height: 1.362; color: ${t.color}; font-weight: ${t.bold ? 700 : 400};` +
        `font-style: ${t.italic ? "italic" : "normal"}; z-index: ${i + 1}; ${outline} ${picked}`;
    },
    frameBoxStyle(t) {
      return t.box ? `background: ${t.box_color}; padding: 0 0.25em; box-decoration-break: clone; -webkit-box-decoration-break: clone;` : "";
    },
    // Remember a phone's <video>/<canvas>, then draw.
    mobileEl(c, kind, el) {
      MOBILE_EL.set(c.id, { ...(MOBILE_EL.get(c.id) || {}), [kind]: el });
      this.drawFrame(c);
    },
    mobileLoaded(c, v) {
      this.mobile.dims[c.id] = [v.videoWidth, v.videoHeight];
      this.mobile.srcError[c.id] = false;
      const src = this.mobileSource(c);
      if (src.from != null) v.currentTime = src.from;
      this.drawFrame(c);
    },
    mobileTime(c, v) {
      const src = this.mobileSource(c);
      let p = v.duration ? v.currentTime / v.duration : 0;
      if (src.from != null) {
        if (v.currentTime >= src.to || v.currentTime < src.from - 1) v.currentTime = src.from; // loop the part
        p = (v.currentTime - src.from) / Math.max(0.1, src.to - src.from);
      }
      this.mobile.progress[c.id] = Math.min(100, Math.max(0, p * 100));
    },
    mobilePlayed(c) {
      this.mobile.playing[c.id] = true;
      const loop = () => {
        const v = MOBILE_EL.get(c.id)?.video;
        if (!v || v.paused || !this.mobile.ids.length) return;
        this.drawFrame(c);
        requestAnimationFrame(loop);
      };
      requestAnimationFrame(loop);
    },
    // Draw the framed picture on the phone's canvas the way ffmpeg will:
    // background (blurred picture / black / colour), then the picture.
    drawFrame(c) {
      const { video: v, canvas: cv } = MOBILE_EL.get(c.id) || {};
      const f = this.mobile.draft;
      if (!cv || !f) return;
      const ctx = cv.getContext("2d");
      const k = cv.width / FRAME_W;
      ctx.filter = "none";
      ctx.fillStyle = f.background === "color" ? f.bg_color : "#000";
      ctx.fillRect(0, 0, cv.width, cv.height);
      if (!v || v.readyState < 2) return;
      const src = this.mobileSource(c);
      const [sx, sy, sw, sh] = src.rect || [0, 0, v.videoWidth, v.videoHeight];
      const [x, y, w, h] = this.frameLayout(sw, sh, f);
      // The live colour sliders (relative to the file's grade); the source video has none.
      const grade = !src.from && this.colorChanged(c) ? `url(#cf-${c.id}) ` : "";
      if (f.background === "blur") {
        const s = Math.max(FRAME_W / sw, FRAME_H / sh) * 1.08; // a bit bigger: blur fades at the edges
        const dw = sw * s;
        const dh = sh * s;
        ctx.filter = `${grade}blur(${39 * k}px) brightness(0.85)`;
        ctx.drawImage(v, sx, sy, sw, sh, ((FRAME_W - dw) / 2) * k, ((FRAME_H - dh) / 2) * k, dw * k, dh * k);
      }
      ctx.filter = grade || "none";
      ctx.drawImage(v, sx, sy, sw, sh, x * k, y * k, w * k, h * k);
      ctx.filter = "none";
    },
    // Does the clip's file already have the editor's frame?
    frameKey(f) {
      if (!f) return "";
      const t = (x) => [x.text, x.x, x.y, x.align, x.size, x.color, !!x.bold, !!x.italic, !!x.outline, !!x.box, x.box_color];
      return JSON.stringify([f.background, f.bg_color, f.video_x ?? 50, f.video_y ?? 50, f.video_scale ?? 100, (f.texts || []).map(t)]);
    },
    frameApplied(c) {
      return this.frameKey(c.frame_rendered) === this.frameKey(this.mobile.draft);
    },
    // Save the editor's frame (or no frame) on each clip shown and re-render it
    // (keeps its colour and music). The file becomes 1080x1920.
    async applyFrame() {
      this.mobile.applying = true;
      try {
        for (const c of this.mobileClips()) {
          if (!this.isPlayable(c) || this.frameApplied(c)) continue;
          // A plain copy (structuredClone can't copy Alpine's reactive proxy).
          c.frame = this.mobile.draft ? JSON.parse(JSON.stringify(this.mobile.draft)) : null;
          await this.rerender(c);
        }
        this.notice = "Re-rendering with your edit. The phones update when each clip is ready.";
      } finally {
        this.mobile.applying = false;
      }
    },

    downloadUrl(c) {
      return `/clips/${c.id}/download?v=${c.file_version || 0}`;
    },

    readyCount() {
      return this.clips.filter((c) => ["ready", "approved"].includes(c.status)).length;
    },

    approvedCount() {
      return this.clips.filter((c) => c.status === "approved" && c.has_file).length;
    },

    // "Save" keeps text/trim edits only. The colour sliders are sent only when
    // the clip is re-rendered, so a saved grade can never sit unapplied while
    // the file (and the download) still has the old look.
    async saveClip(c) {
      await this.patchClip(c);
    },

    async patchClip(c, { withColor = false } = {}) {
      const body = {
        start_s: c.start_s,
        end_s: c.end_s,
        title: c.title,
        description: c.description,
        captions_enabled: c.captions_enabled,
      };
      if (withColor) {
        Object.assign(body, {
          color_contrast: c.color_contrast ?? 0,
          color_brightness: c.color_brightness ?? 0,
          color_saturation: c.color_saturation ?? 0,
          frame: c.frame ?? null, // the text above/below (mobile preview), applied with it
        });
      }
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
      if (!(await this.patchClip(c, { withColor: true }))) return;
      const r = await fetch(`/clips/${c.id}/render`, { method: "POST" });
      if (r.ok) {
        c.status = "rendering";
        this.markColorApplied(c); // the sliders' grade is what's being rendered
        this.watchClip(c);
      }
    },

    // ----- Colour grade (contrast / brightness / saturation) -----
    // _colorApplied = the grade the clip FILE has (server: color_rendered), so
    // "not applied" stays true until a re-render, even after a page reload.
    initColor(c) {
      const g = c.color_rendered || {
        contrast: c.color_contrast,
        brightness: c.color_brightness,
        saturation: c.color_saturation,
      };
      c._colorApplied = { c: g.contrast ?? 0, b: g.brightness ?? 0, s: g.saturation ?? 0 };
    },

    markColorApplied(c) {
      c._colorApplied = {
        c: c.color_contrast ?? 0,
        b: c.color_brightness ?? 0,
        s: c.color_saturation ?? 0,
      };
    },

    // The clip's FILE has a grade (not just the sliders).
    colorOn(c) {
      const a = c._colorApplied || { c: 0, b: 0, s: 0 };
      return !!(a.c || a.b || a.s);
    },

    colorChanged(c) {
      const a = c._colorApplied || { c: 0, b: 0, s: 0 };
      return (
        (c.color_contrast ?? 0) !== a.c ||
        (c.color_brightness ?? 0) !== a.b ||
        (c.color_saturation ?? 0) !== a.s
      );
    },

    // Live preview of the sliders on the player, before any re-render: an SVG
    // colour matrix doing what ffmpeg's eq does (contrast + brightness on
    // luma, saturation on chroma; same slider mapping as
    // ffmpeg.color_eq_filter), relative to the grade the file already has, so
    // a graded clip isn't graded twice. Per channel X:
    // X' = s*X + (k - s)*Y + offset, with Y the BT.709 luma.
    colorPreviewMatrix(c) {
      const eq = (con, bri, sat) => ({ k: 1 + con / 100, b: bri / 250, s: 1 + sat / 100 });
      const a = c._colorApplied || { c: 0, b: 0, s: 0 };
      const file = eq(a.c, a.b, a.s);
      const want = eq(c.color_contrast ?? 0, c.color_brightness ?? 0, c.color_saturation ?? 0);
      const k = want.k / file.k;
      const s = want.s / file.s;
      const offset = 0.5 * (1 - want.k) + want.b - k * (0.5 * (1 - file.k) + file.b);
      const luma = [0.2126, 0.7152, 0.0722];
      const rows = [0, 1, 2].map((ch) => {
        const coef = luma.map((w, i) => (k - s) * w + (i === ch ? s : 0));
        return [...coef, 0, offset];
      });
      return [...rows, [0, 0, 0, 1, 0]].flat().map((v) => v.toFixed(4)).join(" ");
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
        const want = {
          c: this.colorAll.contrast, b: this.colorAll.brightness, s: this.colorAll.saturation,
        };
        const byId = new Map(this.clips.map((c) => [c.id, c]));
        const queued = new Set();
        for (const f of res.clips) {
          const c = byId.get(f.id);
          if (!c) continue;
          queued.add(f.id);
          Object.assign(c, {
            status: f.status,
            error: f.error,
            color_contrast: f.color_contrast,
            color_brightness: f.color_brightness,
            color_saturation: f.color_saturation,
          });
          this.markColorApplied(c);
        }
        // Clips whose file already had this grade were not re-rendered; the
        // server brought their saved sliders in line, so mirror that here.
        for (const c of this.clips) {
          const a = c._colorApplied;
          if (queued.has(c.id) || !this.isPlayable(c) || !a) continue;
          if (a.c === want.c && a.b === want.b && a.s === want.s) {
            Object.assign(c, { color_contrast: want.c, color_brightness: want.b, color_saturation: want.s });
          }
        }
        if (res.skipped) {
          this.notice = `Re-rendering ${res.queued} clip(s) with the new colour. ${res.skipped} skipped (still rendering, or already this colour).`;
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
          if (fresh) this.initColor(c); // the grade the new file really has
          clearInterval(iv);
        }
      }, 2000);
    },

    // Toggle: approve a ready clip, or un-approve an approved one.
    async approve(c) {
      const next = c.status === "approved" ? "ready" : "approved";
      const r = await fetch(`/clips/${c.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ status: next }),
      });
      if (r.ok) c.status = next;
    },

    // Its file leaves the disk too, so it can't be undone.
    async del(c) {
      if (!confirm("Delete this clip? Its file is deleted from this computer.")) return;
      const r = await fetch(`/clips/${c.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ status: "deleted" }),
      });
      if (r.ok) this.clips = this.clips.filter((x) => x.id !== c.id);
      else this.error = (await r.json().catch(() => ({}))).error?.message || "Delete failed";
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
        this.loadBucketMusic(); // a link import is in the bucket now
      }
    },

    readyTracks() {
      return this.tracks.filter((t) => t.status === "ready");
    },

    // Audio files in the bucket, quietly: [] without a bucket (or if it can't be reached).
    async loadBucketMusic() {
      try {
        if (!(await (await fetch("/bucket")).json()).set_up) return;
        const r = await fetch("/bucket/audio");
        if (r.ok) this.bucketMusic = (await r.json()).audio;
      } catch (e) {}
    },
    inLibrary(key) {
      return this.tracks.some((t) => t.bucket?.key === key);
    },
    // What the music pickers offer: the library on this computer, then audio
    // files only in the bucket (☁), brought into the library when picked.
    musicChoices() {
      return [
        ...this.readyTracks().map((t) => ({ value: t.id, label: t.filename, duration_s: t.duration_s })),
        ...this.bucketMusic
          .filter((f) => !this.inLibrary(f.key))
          .map((f) => ({ value: `bucket:${f.key}`, label: `☁ ${f.name}` })),
      ];
    },
    // A picker's new value: a track id as it is, or a bucket file
    // ("bucket:<key>") brought into the library first. Returns the track id
    // to pick ("" if that failed).
    async pickMusic(value) {
      if (!value?.startsWith("bucket:")) return value;
      this.fetchingMusic = true;
      try {
        const t = await this.postJSON("/audio/from-bucket", { key: value.slice("bucket:".length) });
        await this.loadTracks();
        this.notice = `"${t.filename}" brought from the bucket into your music.`;
        return t.id;
      } catch (e) {
        this.error = e.message;
        return "";
      } finally {
        this.fetchingMusic = false;
      }
    },
    isTrack(id) {
      return !!id && !id.startsWith("bucket:");
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
        await Promise.all([this.loadTracks(), this.loadBucketMusic()]);
        this.music.trackId = j.id;
        this.notice = `"${j.filename}" added to your bucket. Pick it and click "Add to all clips", or tick "Background music" on a clip.`;
      } catch (err) {
        this.error = "Audio upload failed: " + err.message;
      } finally {
        this.music.uploading = false;
      }
    },

    async deleteTrack() {
      const t = this.tracks.find((x) => x.id === this.music.trackId);
      if (!t || !confirm(`Delete "${t.filename}" from your bucket, for good?`)) return;
      const r = await fetch(`/audio/${t.id}?from_bucket=true`, { method: "DELETE" });
      if (!r.ok) {
        this.error = (await r.json().catch(() => ({}))).error?.message || "Delete failed";
        return;
      }
      await Promise.all([this.loadTracks(), this.loadBucketMusic()]);
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
    // Where a part sits on the video's timeline bar.
    barStyle(start, end) {
      const dur = this.video?.duration_s || 1;
      const left = Math.max(0, Math.min(100, (start / dur) * 100));
      const width = Math.max(0.5, Math.min(100 - left, ((end - start) / dur) * 100));
      return `left:${left}%;width:${width}%`;
    },
    // 83.5 -> "1:23.5"; 3723 -> "1:02:03.0"
    fmtTime(s) {
      if (s === null || s === undefined || Number.isNaN(s)) return "";
      const t = Math.max(0, Math.round(s * 10) / 10);
      const h = Math.floor(t / 3600);
      const m = Math.floor((t % 3600) / 60);
      const sec = (t % 60).toFixed(1).padStart(4, "0");
      return h ? `${h}:${String(m).padStart(2, "0")}:${sec}` : `${m}:${sec}`;
    },
    parseTime(text) {
      const parts = String(text).trim().split(":");
      if (parts.length > 3 || parts.some((p) => p.trim() === "" || Number.isNaN(Number(p)))) {
        return NaN;
      }
      return parts.reduce((acc, p) => acc * 60 + Number(p), 0);
    },
    platformLabel(key) {
      return this.platforms.find((p) => p.key === key)?.label || key;
    },
    stageLabel(stage) {
      const m = {
        queued: "Queued", transcribing: "Transcribing",
        analyzing: "Analysing scenes & audio", planning_clips: "Planning clips",
        cutting: "Cutting", reframing: "Reframing",
        rendering: "Rendering", finalizing: "Finalising", done: "Done",
      };
      return m[stage] || stage || "";
    },
    fmtSize(b) {
      if (!b) return "";
      return b >= 1e9 ? `${(b / 1e9).toFixed(1)} GB` : `${Math.round(b / 1e6)} MB`;
    },
    // Disk sizes: KB for small files, then MB / GB.
    fmtBytes(b) {
      if (!b) return "0 MB";
      if (b < 1e6) return `${Math.max(1, Math.round(b / 1e3))} KB`;
      return this.fmtSize(b);
    },
    fmtWhen(iso) {
      return new Date(iso).toLocaleString();
    },
    fmtDur(s) {
      if (!s) return "";
      const m = Math.floor(s / 60), sec = Math.round(s % 60);
      return `${m}m ${sec}s`;
    },
  };
}
