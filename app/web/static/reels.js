// "Create a reel": a nested Alpine component inside clipforge(), so it shares
// its toasts (error/notice), the music library (tracks) and helpers.
function reelMaker() {
  // The voice sample playing ("Listen"). Kept out of Alpine's reactive data:
  // calling an <audio>'s methods through its proxy fails.
  let sampleAudio = null;
  return {
    options: {
      platforms: [], languages: [], styles: [], script_types: [], description_max: 1500,
      duration: { min: 15, max: 300, default: 35 }, max_scenes: 20,
      timing: { picture_s: 58, load_s: 5, script_s: 25, script_s_per_s: 0.5, render_s_per_s: 0.35 },
    },
    form: {
      platform: "instagram", language: "hi", voice: "", style: "cinematic", duration_s: 35,
      script_type: "story", topic: "", description: "",
    },
    creating: false,
    reels: [],
    reel: null, // the open reel, as the server has it
    draft: null, // its script being edited
    gen: { voice: "", style: "cinematic", musicId: "", musicVolume: 20 },
    idea: { script_type: "story", description: "" }, // the open reel's, for "Write again"
    sample: { key: "", state: "" }, // the voice sample: state "loading" | "playing"
    nameBefore: "", // a character's name while it is being edited (scenes use it)
    // Adding music to the library from the reel editor (upload or a link).
    // pendingId: a link import still downloading; picked once it is ready.
    reelMusic: { uploading: false, link: "", grabbing: false, deleting: false, pendingId: null, suggesting: false },
    saving: false,
    bucketUploading: null, // id of the reel being copied to the bucket
    _reelPoll: null,

    async initReels() {
      try {
        const r = await fetch("/reels/options");
        this.options = await r.json();
        this.form.duration_s = this.options.duration.default;
        this.form.voice = this.voicesFor(this.form.language)[0]?.key || "";
      } catch (e) {
        this.error = "Could not load reel options";
      }
      // The music library (tracks) is loaded and polled by clipforge().
      this.$watch("tracks", () => this.pickPendingTrack());
      window.addEventListener("clipforge:reels-changed", () => this.loadReels());
      await this.loadReels();
    },

    voicesFor(lang) {
      return this.options.languages.find((l) => l.key === lang)?.voices || [];
    },
    languageLabel(key) {
      return this.options.languages.find((l) => l.key === key)?.label || key;
    },
    reelPlatformLabel(key) {
      return this.options.platforms.find((p) => p.key === key)?.label || key;
    },
    // ▶ next to a voice: a short line in that voice (made on the first click,
    // ~2-3 s, then kept). A second click stops it.
    listen(lang, voice) {
      const key = `${lang}/${voice}`;
      const again = this.sample.key === key;
      this.stopSample();
      if (again || !voice) return;
      const audio = new Audio(`/reels/voices/${encodeURIComponent(lang)}/${encodeURIComponent(voice)}/sample`);
      sampleAudio = audio;
      this.sample = { key, state: "loading" };
      audio.addEventListener("playing", () => { if (sampleAudio === audio) this.sample.state = "playing"; });
      audio.addEventListener("ended", () => { if (sampleAudio === audio) this.stopSample(); });
      audio.addEventListener("error", () => {
        if (sampleAudio !== audio) return;
        this.stopSample();
        this.error = "Could not play this voice. Is Docker running?";
      });
      audio.play().catch(() => {});
    },
    stopSample() {
      sampleAudio?.pause();
      sampleAudio = null;
      this.sample = { key: "", state: "" };
    },
    listenIcon(lang, voice) {
      if (this.sample.key !== `${lang}/${voice}`) return "▶";
      return this.sample.state === "loading" ? "…" : "■";
    },
    scriptTypeLabel(key) {
      return this.options.script_types.find((t) => t.key === key)?.label || key;
    },
    styleLabel(key) {
      return this.options.styles.find((s) => s.key === key)?.label || key;
    },

    // Rough time to make a reel, shown in the picture style menus. Every style
    // takes about as long; what counts is how many pictures are drawn.
    fmtEta(s) {
      return s < 60 ? `~${Math.max(5, Math.round(s / 5) * 5)} s` : `~${Math.round(s / 60)} min`;
    },
    // As reel_script.scene_count: a picture every ~7 s (at most 8), past ~3 minutes one every 20 s.
    sceneCount(d) {
      return Math.max(3, Math.min(8, Math.round(d / 7)), Math.round(d / 20));
    },
    picturesEta(n) {
      const t = this.options.timing;
      return n ? t.load_s + n * t.picture_s : 0;
    },
    // A new reel: the script, every picture, then the video.
    newReelEta() {
      const t = this.options.timing, d = this.form.duration_s;
      return t.script_s + t.script_s_per_s * d + this.picturesEta(this.sceneCount(d)) + t.render_s_per_s * d;
    },
    // "Generate video" in picture style `key`: the reel's own style redraws only
    // new or changed scenes, another style redraws every picture.
    generateEta(key) {
      if (!this.reel || !this.draft) return 0;
      const saved = this.reel.script || { scenes: [] };
      const n = key !== this.reel.style
        ? this.draft.scenes.length
        : this.draft.scenes.filter((s, i) => !this.reel.scene_pictures[i] || !saved.scenes[i]
          || this.pictureSource(s, this.draft) !== this.pictureSource(saved.scenes[i], saved)).length;
      return this.picturesEta(n) + this.options.timing.render_s_per_s * this.estimateS();
    },
    // Being worked on. A "stalled" reel was cut off by a restart: free to retry.
    busy(r) {
      return !!r && ["scripting", "generating"].includes(r.status) && !r.stalled;
    },
    statusLabel(r) {
      if (r.stalled) return "Stopped";
      return {
        scripting: "Writing script…", script_ready: "Script ready", generating: "Making video…",
        ready: "Ready", failed: "Failed",
      }[r.status] || r.status;
    },

    async loadReels() {
      try {
        const r = await fetch("/reels");
        if (r.ok) this.reels = await r.json();
      } catch (e) {}
    },

    async createReel() {
      if (this.form.topic.trim().length < 3) {
        this.error = "Write a topic first (what the reel is about).";
        return;
      }
      this.creating = true;
      try {
        const reel = await this.postJSON("/reels", this.form);
        this.form.topic = "";
        this.form.description = "";
        this.reels = [reel, ...this.reels];
        this.open(reel);
      } catch (e) {
        this.error = e.message;
      } finally {
        this.creating = false;
      }
    },

    open(reel) {
      this.reel = reel;
      this.draft = reel.script ? this.toDraft(reel.script) : null;
      this.idea = { script_type: reel.script_type, description: reel.description };
      this.gen = {
        voice: reel.voice,
        style: reel.style,
        musicId: reel.music_id || "",
        musicVolume: reel.music_volume,
      };
      this.watchReel();
    },

    toDraft(script) {
      const d = JSON.parse(JSON.stringify(script));
      d.hashtagsText = (d.hashtags || []).map((h) => "#" + h).join(" ");
      return d;
    },
    fromDraft() {
      const { hashtagsText, ...script } = this.draft;
      script.hashtags = hashtagsText.split(/[\s,]+/).map((h) => h.replace(/^#/, "")).filter(Boolean);
      return script;
    },
    // Edits not saved to the server yet (both sides keep the same key order).
    dirty() {
      if (!this.draft || !this.reel?.script) return false;
      return JSON.stringify(this.fromDraft()) !== JSON.stringify(this.reel.script);
    },

    // Poll the open reel while it is being worked on.
    watchReel() {
      if (this._reelPoll) clearInterval(this._reelPoll);
      this._reelPoll = null;
      if (!this.busy(this.reel)) return;
      this._reelPoll = setInterval(async () => {
        if (!this.reel) return clearInterval(this._reelPoll);
        try {
          const r = await fetch(`/reels/${this.reel.id}`);
          if (r.status === 404) {
            clearInterval(this._reelPoll);
            this.reel = null;
            return this.loadReels();
          }
          const fresh = await r.json();
          const scriptArrived = this.reel.status === "scripting" && fresh.status !== "scripting";
          this.reel = fresh;
          if (scriptArrived && fresh.script) this.draft = this.toDraft(fresh.script);
          this.updateListItem(fresh);
          if (!this.busy(fresh)) {
            clearInterval(this._reelPoll);
            this._reelPoll = null;
            if (fresh.status === "ready") this.notice = "Your reel is ready.";
          }
        } catch (e) {}
      }, 2000);
    },

    updateListItem(fresh) {
      const i = this.reels.findIndex((r) => r.id === fresh.id);
      if (i >= 0) this.reels[i] = fresh;
      else this.reels.unshift(fresh);
    },

    async putScript() {
      const r = await fetch(`/reels/${this.reel.id}/script`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(this.fromDraft()),
      });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) {
        const detail = j.error?.details?.[0];
        throw new Error(detail ? `${j.error.message}: ${detail.loc?.slice(1).join(" ")} ${detail.msg}` : j.error?.message || `HTTP ${r.status}`);
      }
      this.reel = j;
      this.draft = this.toDraft(j.script);
      this.updateListItem(j);
    },

    async saveScript() {
      this.saving = true;
      try {
        await this.putScript();
        this.notice = "Script saved.";
      } catch (e) {
        this.error = e.message;
      } finally {
        this.saving = false;
      }
    },

    async rewrite() {
      if (!confirm("Write a completely new script? Your edits to this one will be lost.")) return;
      try {
        this.reel = await this.postJSON(`/reels/${this.reel.id}/rewrite`, this.idea);
        this.updateListItem(this.reel);
        this.watchReel();
      } catch (e) {
        this.error = e.message;
      }
    },

    async generate() {
      this.saving = true;
      try {
        await this.putScript(); // the video is made from the saved script
        this.reel = await this.postJSON(`/reels/${this.reel.id}/generate`, {
          voice: this.gen.voice,
          style: this.gen.style,
          music_id: this.gen.musicId || null,
          music_volume: this.gen.musicVolume,
        });
        this.updateListItem(this.reel);
        this.watchReel();
      } catch (e) {
        this.error = e.message;
      } finally {
        this.saving = false;
      }
    },

    async newPicture(i) {
      try {
        await this.putScript();
        this.reel = await this.postJSON(`/reels/${this.reel.id}/scenes/${i}/new-picture`, {});
        this.updateListItem(this.reel);
        this.watchReel();
      } catch (e) {
        this.error = e.message;
      }
    },

    async deleteReel() {
      const kept = this.reel.bucket ? "\n\nIts copy in the bucket is kept." : "";
      if (!confirm("Delete this reel and its video?" + kept)) return;
      try {
        await this.removeReel(this.reel.id);
      } catch (e) {
        this.error = e.message;
      }
    },

    // Deletes the reel's row and every file of it (pictures, voice, video) on
    // the server. Music tracks are never touched.
    async removeReel(id) {
      const r = await fetch(`/reels/${id}`, { method: "DELETE" });
      if (!r.ok && r.status !== 404) throw new Error("Delete failed");
      if (this._reelPoll) clearInterval(this._reelPoll);
      this.reel = null;
      this.draft = null;
      this.reels = this.reels.filter((x) => x.id !== id);
    },

    // Save the video on this computer, then free the server's disk: the reel
    // is deleted only once the whole file has arrived.
    async downloadAndDelete() {
      const msg = "Download the video, then delete this reel and all its files " +
        "(pictures, voice, video) from the server to free space?\n\nYour music tracks are kept.";
      if (!confirm(msg)) return;
      this.saving = true;
      try {
        const r = await fetch(`/reels/${this.reel.id}/video?download=1`);
        if (!r.ok) throw new Error(`Download failed (HTTP ${r.status}); nothing was deleted.`);
        const blob = await r.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = this.filenameFrom(r.headers.get("content-disposition")) || "reel.mp4";
        document.body.appendChild(a);
        a.click();
        a.remove();
        setTimeout(() => URL.revokeObjectURL(url), 60000);
        await this.removeReel(this.reel.id);
        this.notice = "Video downloaded. The reel and its files were deleted from the server.";
      } catch (e) {
        this.error = e.message;
      } finally {
        this.saving = false;
      }
    },

    // Its copy was deleted in the Bucket tab: not in the bucket any more.
    forgetBucketCopy(key) {
      for (const r of [this.reel, ...this.reels]) if (r?.bucket?.key === key) r.bucket = null;
    },

    // Copy the video to the bucket (BUCKET_* in .env); the server answers once
    // the upload has finished. Uploading again replaces the copy.
    async uploadReelToBucket() {
      const id = this.reel.id;
      this.bucketUploading = id;
      try {
        const fresh = await this.postJSON(`/reels/${id}/upload-to-bucket`, {});
        if (this.reel?.id === id) this.reel = fresh;
        this.updateListItem(fresh);
        this.notice = "Uploaded to the bucket.";
      } catch (e) {
        this.error = e.message;
      } finally {
        this.bucketUploading = null;
      }
    },

    // File name from a Content-Disposition header (UTF-8 form first: Hindi titles).
    filenameFrom(header) {
      if (!header) return "";
      const utf8 = /filename\*=utf-8''([^;]+)/i.exec(header);
      if (utf8) return decodeURIComponent(utf8[1]);
      return /filename="?([^";]+)"?/i.exec(header)?.[1] || "";
    },

    // ----- Suggested music: the LLM says what kind fits; the user finds it online -----
    async suggestMusic() {
      this.reelMusic.suggesting = true;
      try {
        const fresh = await this.postJSON(`/reels/${this.reel.id}/music-suggestion`, {});
        // Keep unsaved edits: only the suggestion comes from the server.
        this.draft.music = fresh.script.music;
        this.reel = fresh;
        this.updateListItem(fresh);
      } catch (e) {
        this.error = e.message;
      } finally {
        this.reelMusic.suggesting = false;
      }
    },
    youtubeSearchUrl() {
      return "https://www.youtube.com/results?search_query=" + encodeURIComponent(this.draft.music.search);
    },
    async copyMusicSearch() {
      try {
        await navigator.clipboard.writeText(this.draft.music.search);
        this.notice = "Search copied.";
      } catch (e) {
        this.error = "Could not copy. Select the text and copy it yourself.";
      }
    },

    // ----- Music library, from the reel editor -----
    async uploadReelMusic(e) {
      const f = e.target.files[0];
      e.target.value = "";
      if (!f) return;
      this.reelMusic.uploading = true;
      try {
        const fd = new FormData();
        fd.append("file", f);
        const r = await fetch("/audio/upload", { method: "POST", body: fd });
        const j = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(j.error?.message || `HTTP ${r.status}`);
        await Promise.all([this.loadTracks(), this.loadBucketMusic()]);
        this.gen.musicId = j.id;
        this.notice = `"${j.filename}" added to your bucket and picked for this reel.`;
      } catch (err) {
        this.error = "Audio upload failed: " + err.message;
      } finally {
        this.reelMusic.uploading = false;
      }
    },

    // Grab only the audio of a link (YouTube, SoundCloud, ...). It downloads
    // in the background and is picked for this reel once it is ready.
    async grabReelMusic() {
      if (!this.reelMusic.link || this.reelMusic.grabbing) return;
      this.reelMusic.grabbing = true;
      try {
        const t = await this.postJSON("/audio/from-url", { url: this.reelMusic.link });
        this.reelMusic.link = "";
        this.reelMusic.pendingId = t.id;
        await this.loadTracks();
      } catch (e) {
        this.error = e.message;
      } finally {
        this.reelMusic.grabbing = false;
      }
    },

    pickPendingTrack() {
      const t = this.tracks.find((x) => x.id === this.reelMusic.pendingId);
      if (!t || t.status === "processing") return;
      this.reelMusic.pendingId = null;
      if (t.status === "ready") {
        this.gen.musicId = t.id;
        this.notice = `"${t.filename}" added and picked for this reel.`;
      }
    },

    // Remove the picked track from the music library (its own button, away
    // from the reel's Delete).
    async deleteReelMusic() {
      const t = this.tracks.find((x) => x.id === this.gen.musicId);
      if (!t) return;
      const msg = `Delete "${t.filename}" from your bucket, for good?\n\n` +
        "Reels already made keep their sound; this track disappears from every music list.";
      if (!confirm(msg)) return;
      this.reelMusic.deleting = true;
      try {
        const r = await fetch(`/audio/${t.id}?from_bucket=true`, { method: "DELETE" });
        if (!r.ok) {
          throw new Error((await r.json().catch(() => ({}))).error?.message || "Delete failed");
        }
        this.gen.musicId = "";
        await Promise.all([this.loadTracks(), this.loadBucketMusic()]);
        this.notice = `"${t.filename}" deleted from your bucket.`;
      } catch (e) {
        this.error = e.message;
      } finally {
        this.reelMusic.deleting = false;
      }
    },

    useHook(h) {
      this.draft.scenes[0].narration = h;
    },
    addScene() {
      const scene = { narration: "", image_prompt: "" };
      // Scripts with characters: the same keys as the server's (it compares them).
      if (this.draft.characters !== undefined) Object.assign(scene, { characters: [], crowd: false });
      this.draft.scenes.push(scene);
    },

    // ----- Characters (the same look in every picture) -----
    addCharacter() {
      this.draft.characters.push({ name: `Person ${this.draft.characters.length + 1}`, look: "" });
    },
    removeCharacter(k) {
      const [gone] = this.draft.characters.splice(k, 1);
      for (const s of this.draft.scenes) {
        if (s.characters) s.characters = s.characters.filter((n) => n !== gone.name);
      }
    },
    // A new name for a character: its scenes follow.
    renameCharacter(before, after) {
      if (!before || before === after) return;
      for (const s of this.draft.scenes) {
        if (s.characters) s.characters = s.characters.map((n) => (n === before ? after : n));
      }
    },
    // In or out of a scene's picture; at most 3 (more come out with extra arms and faces).
    toggleInScene(s, name) {
      if (s.characters.includes(name)) {
        s.characters = s.characters.filter((n) => n !== name);
      } else if (s.characters.length < 3) {
        s.characters = [...s.characters, name];
      } else {
        this.notice = "At most 3 characters in a picture (2 or fewer come out best).";
      }
    },
    // What a scene's picture is drawn from (as reel_render.scene_prompt): a change redraws it.
    pictureSource(s, script) {
      const looks = Object.fromEntries((script.characters || []).map((c) => [c.name, c.look]));
      return JSON.stringify([s.image_prompt, (s.characters || []).map((n) => [n, looks[n]]), !!s.crowd,
        script.setting || ""]);
    },
    removeScene(i) {
      this.draft.scenes.splice(i, 1);
    },

    words(text) {
      return (text || "").trim().split(/\s+/).filter(Boolean).length;
    },
    // Rough spoken length: words at the voice's pace plus the pauses between scenes.
    estimateS() {
      if (!this.draft) return 0;
      const lang = this.options.languages.find((l) => l.key === this.reel.language);
      // A slower voice ("calm") takes longer for the same words; a story is read a
      // little slower (0.92), with 0.8 s between scenes (reel_render.STORY_*).
      const story = this.reel.script_type === "story";
      const speed = (lang?.voices.find((v) => v.key === this.gen.voice)?.speed || 1) * (story ? 0.92 : 1);
      const wps = (lang?.words_per_s || 2.8) * speed;
      const words = this.draft.scenes.reduce((n, s) => n + this.words(s.narration), 0);
      const gaps = (this.draft.scenes.length - 1) * (story ? 0.8 : 0.25);
      return Math.round(words / wps + gaps + 0.8);
    },
    pictureUrl(i) {
      return `/reels/${this.reel.id}/scenes/${i}/picture?v=${encodeURIComponent(this.reel.updated_at)}`;
    },
    reelVideoUrl(r) {
      return `/reels/${r.id}/video?v=${r.video_version}`;
    },
    async copyCaption() {
      const text = [this.draft.caption, this.fromDraft().hashtags.map((h) => "#" + h).join(" ")]
        .filter(Boolean).join("\n\n");
      try {
        await navigator.clipboard.writeText(text);
        this.notice = "Caption and hashtags copied.";
      } catch (e) {
        this.error = "Could not copy. Select the text and copy it yourself.";
      }
    },
  };
}
