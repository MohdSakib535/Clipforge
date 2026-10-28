// "Reel from a video": paste a link or upload a video, trim the parts you want
// by hand and get 9:16 reels (YouTube Shorts / Facebook Reel), with an
// optional transcript of each part, colour and background music.
//
// A nested Alpine component inside clipforge(), like reelMaker(). It has its
// OWN copy of the clip flow's state below (video, job, clips, ...): the
// clipforge() methods it calls - upload, link import, music, colour, clip
// editing - work on whichever component calls them, so here they act on this
// screen and "Clip a video" is left alone. Toasts (error/notice), the music
// library (tracks, music) and platforms are shared.
function reelFromVideo() {
  return {
    // ----- State the shared clipforge() methods read and write -----
    step: "upload", // "upload", then the trim screen (startUpload/importLink set "rights")
    source: "link", // "link" | "file"
    link: { url: "", checking: false, importing: false, info: null, quality: "best" },
    file: null,
    uploading: false,
    uploadPct: 0,
    video: null,
    rightsChecked: false,
    job: null, // every reel of this video is in this one job; more trims are added to it
    clips: [],
    colorAll: { contrast: 0, brightness: 0, saturation: 0, busy: false },
    _poll: null,
    _watch: null,
    _upload: null,
    _uploadDone: null,
    _uploadVideoId: null,

    // ----- Trimming -----
    cut: { start: 0, end: 0 }, // the part being picked (seconds)
    trims: [], // parts added, waiting to be made: [{ start_s, end_s }]
    reelPlatforms: ["youtube_shorts", "facebook_reel"],
    withTranscript: true,
    making: false,
    playerError: false, // the browser can't play this file: times are typed instead
    _stopAt: null, // "Play this part" pauses here

    // Vertical presets from platforms.yaml (YouTube Shorts, Facebook Reel).
    reelPresets() {
      return this.platforms.filter((p) => p.is_vertical);
    },
    chosenPresets() {
      return this.reelPresets().filter((p) => this.reelPlatforms.includes(p.key));
    },
    toggleReelPlatform(key) {
      const i = this.reelPlatforms.indexOf(key);
      if (i >= 0) this.reelPlatforms.splice(i, 1);
      else this.reelPlatforms.push(key);
    },
    // The longest part every chosen platform accepts.
    maxLen() {
      const presets = this.chosenPresets().length ? this.chosenPresets() : this.reelPresets();
      return Math.min(...presets.map((p) => p.length_max_s), 60);
    },

    // First part offered once the video is ready: the start, ~30 s long.
    ensureCut() {
      if (this.cut.end > 0) return;
      const dur = this.video?.duration_s || 30;
      this.cut = { start: 0, end: Math.round(Math.min(dur, 30, this.maxLen()) * 10) / 10 };
    },

    // Why a part can't be made for the chosen platforms ("" = fine). Same
    // rules as the server (lengths rounded to 0.1 s).
    partProblem(start, end) {
      const dur = this.video?.duration_s;
      if (!(end > start)) return "The end must be after the start.";
      if (start < 0 || (dur && end > dur + 0.5)) return "This part is outside the video.";
      const len = Math.round((end - start) * 10) / 10;
      for (const p of this.chosenPresets()) {
        if (len < p.length_min_s || len > p.length_max_s) {
          return `${len.toFixed(1)} s: ${p.label} allows ${p.length_min_s}-${p.length_max_s} s.`;
        }
      }
      return "";
    },
    cutProblem() {
      return this.partProblem(this.cut.start, this.cut.end);
    },
    cutLen() {
      return Math.max(0, this.cut.end - this.cut.start);
    },

    // ----- Player -----
    playerTime() {
      const v = this.$refs.srcPlayer;
      return v ? Math.round(v.currentTime * 10) / 10 : null;
    },
    markStart() {
      const t = this.playerTime();
      if (t === null) return;
      this.cut.start = t;
      if (this.cut.end <= t) {
        const dur = this.video?.duration_s || t + 30;
        this.cut.end = Math.round(Math.min(dur, t + Math.min(30, this.maxLen())) * 10) / 10;
      }
    },
    markEnd() {
      const t = this.playerTime();
      if (t !== null) this.cut.end = t;
    },
    playPart(start, end) {
      const v = this.$refs.srcPlayer;
      if (!v) return;
      v.currentTime = start;
      this._stopAt = end;
      v.play().catch(() => {});
    },
    onTime() {
      const v = this.$refs.srcPlayer;
      if (v && this._stopAt !== null && v.currentTime >= this._stopAt) {
        v.pause();
        this._stopAt = null;
      }
    },
    // A typed time ("1:23.5", "83.5" or "1:02:03"); the player jumps there.
    setCut(which, e) {
      const t = this.parseTime(e.target.value);
      if (Number.isNaN(t)) {
        this.error = "Type a time like 1:23.5 (minutes:seconds) or 83.5 (seconds).";
      } else {
        this.cut[which] = Math.round(t * 10) / 10;
        const v = this.$refs.srcPlayer;
        if (v && !this.playerError) v.currentTime = this.cut[which];
      }
      e.target.value = this.fmtTime(this.cut[which]);
    },

    // ----- Parts to make -----
    addTrim() {
      const problem = this.cutProblem();
      if (problem) {
        this.error = problem;
        return;
      }
      const dur = this.video?.duration_s || this.cut.end;
      const start = this.cut.start;
      const end = Math.min(this.cut.end, dur);
      this.trims.push({ start_s: start, end_s: end });
      // Offer the next part: starts where this one ends, same length.
      this.cut = { start: end, end: Math.round(Math.min(dur, 2 * end - start) * 10) / 10 };
    },
    // The parts "Make reels" sends: the list, or else the part picked now.
    trimsToMake() {
      if (this.trims.length) return this.trims;
      return [{ start_s: this.cut.start, end_s: this.cut.end }];
    },
    reelCount() {
      return this.trimsToMake().length * this.reelPlatforms.length;
    },
    jobBusy() {
      return !!this.job && ["queued", "running"].includes(this.job.status);
    },
    // Why "Make reels" is disabled ("" = it isn't).
    makeProblem() {
      if (this.jobBusy()) return "Wait until the reels below are made.";
      if (!this.reelPlatforms.length) return "Pick YouTube Shorts, Facebook Reel or both.";
      const parts = this.trimsToMake();
      for (const [i, t] of parts.entries()) {
        const problem = this.partProblem(t.start_s, t.end_s);
        if (problem) return parts.length > 1 ? `Part ${i + 1}: ${problem}` : problem;
      }
      if (!this.video?.rights_confirmed_at && !this.rightsChecked) {
        return "Tick the box to confirm you may use this video.";
      }
      return "";
    },

    async makeReels() {
      if (this.making || this.makeProblem()) return;
      this.making = true;
      try {
        if (!this.video.rights_confirmed_at) {
          this.video = await this.postJSON(`/videos/${this.video.id}/confirm-rights`, {
            confirmed: this.rightsChecked,
          });
        }
        const body = {
          trims: this.trimsToMake(),
          platforms: this.reelPlatforms,
          transcribe: this.withTranscript,
        };
        this.job = this.job
          ? await this.postJSON(`/jobs/${this.job.id}/trims`, body)
          : await this.postJSON(`/videos/${this.video.id}/jobs`, {
              ...body,
              captions: { enabled: this.withTranscript },
            });
        this.trims = [];
        this.pollReels();
      } catch (e) {
        this.error = e.message;
      } finally {
        this.making = false;
      }
    },

    // Poll the job while it runs; reels appear as each one finishes.
    pollReels() {
      if (this._poll) clearInterval(this._poll);
      this._poll = setInterval(async () => {
        try {
          const r = await fetch(`/jobs/${this.job.id}`);
          if (!r.ok) return;
          this.job = await r.json();
          await this.refreshClips();
          if (!this.jobBusy()) {
            clearInterval(this._poll);
            this._poll = null;
            if (this.job.status === "completed") this.notice = "Your reels are ready.";
          }
        } catch (e) {}
      }, 1500);
    },

    // Newest first: reels made later show up right under "Make reels". A
    // Hindi dub comes right after its original.
    reelCards() {
      return [...this.clips].sort(
        (a, b) =>
          b.rank - a.rank ||
          a.platform.localeCompare(b.platform) ||
          (a.dub_language ? 1 : 0) - (b.dub_language ? 1 : 0)
      );
    },

    // ----- Translate & dub (Hindi): the local LLM translates, Kokoro reads it out -----
    // c._tr: { busy, making, voices, voice, lines: [{start_s, end_s, text, translation, roman}] }
    async translateClip(c) {
      const prev = c._tr || {};
      const edited = (prev.lines || []).some((l) => l.translation !== l._orig);
      if (edited && !confirm("Translate again? Your changes to the Hindi will be lost.")) return;
      c._tr = { ...prev, busy: true };
      try {
        const t = await this.postJSON(`/clips/${c.id}/translate`, { language: "hi" });
        c._tr = {
          busy: false,
          making: false,
          voices: t.voices,
          voice: prev.voice || t.voices[0]?.key || "",
          lines: t.lines.map((l) => ({ ...l, _orig: l.translation })),
        };
      } catch (e) {
        this.error = e.message;
        c._tr = { ...prev, busy: false };
      }
    },

    // A new reel next to this one, with the edited Hindi read out instead of
    // the original sound. Its card shows "Making…" until it is ready.
    async makeDub(c) {
      const tr = c._tr;
      tr.making = true;
      try {
        await this.postJSON(`/clips/${c.id}/dub`, {
          language: "hi",
          voice: tr.voice,
          lines: tr.lines.map((l) => ({ start_s: l.start_s, end_s: l.end_s, text: l.translation })),
        });
        await this.refreshClips();
        this.watchRendering();
        this.notice = "Making the Hindi reel. It appears next to this one.";
      } catch (e) {
        this.error = e.message;
      } finally {
        tr.making = false;
      }
    },

    async copyTranslation(c) {
      try {
        await navigator.clipboard.writeText(c._tr.lines.map((l) => l.translation).join("\n"));
        this.notice = "Hindi copied.";
      } catch (e) {
        this.error = "Could not copy. Select the text and copy it yourself.";
      }
    },

    async copyTranscript(c) {
      try {
        await navigator.clipboard.writeText(c.transcript);
        this.notice = "Transcript copied.";
      } catch (e) {
        this.error = "Could not copy. Select the text and copy it yourself.";
      }
    },

    // Another video. Reels still being made are stopped and deleted (after a
    // confirm); finished reels stay on the server but leave this screen.
    async startOver() {
      const running = this.runningWork();
      if (running && this.jobBusy()) {
        running.question =
          "Reels are still being made.\n\nStop and delete every reel made from this video?";
      }
      if (running && !confirm(running.question)) return;
      if (!running && this.clips.length && !confirm(
        "Start over with another video?\n\nDownload your reels first: they won't be shown here again."
      )) return;
      if (this._poll) clearInterval(this._poll);
      if (this._watch) clearInterval(this._watch);
      if (running) {
        try {
          await running.stop();
        } catch (e) {} // already finished or gone: nothing left to stop
      }
      Object.assign(this, {
        step: "upload", file: null, uploading: false, uploadPct: 0,
        link: { url: "", checking: false, importing: false, info: null, quality: "best" },
        video: null, rightsChecked: false, job: null, clips: [],
        colorAll: { contrast: 0, brightness: 0, saturation: 0, busy: false },
        cut: { start: 0, end: 0 }, trims: [], making: false, playerError: false,
        _poll: null, _watch: null, _stopAt: null,
      });
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
  };
}
