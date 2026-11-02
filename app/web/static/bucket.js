// "Bucket": send videos from this computer straight to the bucket, into their
// own folder (<prefix>/uploads/clips/ or uploads/reels/: the part of the app
// they are for), apart from the reels; and see, open, download and delete
// every video in the bucket, in two lists: "Clip a video" and "Create a reel". Each file goes up in parts through the
// API, so the bucket needs no CORS setup and the bar shows real progress.
// Music goes straight to <prefix>/audio/ too: a file (in parts, as videos) or
// the audio of a link (the server fetches it and deletes it once it is in the
// bucket), so nothing is kept on this computer. Every audio file in the bucket
// can also be renamed. The music pickers inside the app offer them (☁).
// A nested Alpine component inside clipforge(), so it shares its toasts
// (error/notice), the music lists (tracks, bucketMusic) and helpers (fmtBytes, fmtWhen, ...).
function bucketUploads() {
  return {
    status: null, // GET /bucket: { set_up, bucket, endpoint, uploads_folder, upload_folders, reels_folders }
    files: [], // every video in the bucket, any folder, newest first
    truncated: false, // a very big bucket: only part of it is listed
    listing: false,
    listError: "",
    // The two lists (and the two places an upload can go).
    sections: [
      { key: "clips", title: "Clip a video", about: "What you uploaded for Clip a video, and any folder ClipForge doesn't use." },
      { key: "reels", title: "Create a reel", about: "Reels this app sent to the bucket (from a topic or from a video) and what you uploaded for Create a reel." },
    ],
    uploadTo: "clips", // the list picked videos go to: "clips" | "reels"
    filters: { clips: { text: "", area: "all" }, reels: { text: "", area: "all" } },
    shown: { clips: 100, reels: 100 }, // rows on screen in each list; "Show more" adds 100
    deleting: null, // key of the video or audio file being deleted
    chosen: [], // keys ticked in the video lists, to download or delete together
    bulkJob: null, // { section, action: "download" | "delete", done, total } while one runs
    // Audio files in the bucket, any folder, newest first: bucketMusic, kept
    // by clipforge() (its music pickers offer them too).
    audioTruncated: false,
    audioListing: false,
    audioError: "",
    audioText: "", // search
    renaming: { key: "", name: "", busy: false }, // the audio file being renamed
    // Adding music: file is the one going up ({ file, state, pct, key, error }).
    musicAdd: { file: null, link: "", grabbing: false },
    _importing: [], // ids of link imports for the bucket still downloading
    // Picked videos: { id, file, state: waiting | uploading | done | failed | cancelled, pct, key, error }
    queue: [],
    _nextId: 1,
    sending: false,
    _send: null, // AbortController of the uploads in progress

    initBucket() {
      // Loaded when the tab is opened (the list asks the bucket each time).
      this.$watch("mode", (m) => m === "bucket" && this.refreshBucket());
      this.$watch("tracks", () => this.checkBucketImports());
    },

    async refreshBucket() {
      try {
        this.status = await (await fetch("/bucket")).json();
      } catch (e) {
        this.error = "Could not load the bucket settings";
        return;
      }
      if (this.status.set_up) await Promise.all([this.loadFiles(), this.loadAudio()]);
    },

    async loadAudio() {
      this.audioListing = true;
      this.audioError = "";
      try {
        const r = await fetch("/bucket/audio");
        const j = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(j.error?.message || `HTTP ${r.status}`);
        this.bucketMusic = j.audio;
        this.audioTruncated = j.truncated;
      } catch (e) {
        this.audioError = e.message;
      } finally {
        this.audioListing = false;
      }
    },

    async loadFiles() {
      this.listing = true;
      this.listError = "";
      try {
        const r = await fetch("/bucket/videos");
        const j = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(j.error?.message || `HTTP ${r.status}`);
        this.files = j.videos;
        this.truncated = j.truncated;
        const keys = new Set(j.videos.map((v) => v.key));
        this.chosen = this.chosen.filter((k) => keys.has(k)); // gone from the bucket
      } catch (e) {
        this.listError = e.message;
      } finally {
        this.listing = false;
      }
    },

    pickBucketFiles(e) {
      const picked = [...e.target.files];
      e.target.value = "";
      const kept = this.queue.filter((q) => ["waiting", "failed"].includes(q.state));
      this.queue = [...kept, ...picked.map((file) => ({ id: this._nextId++, file, state: "waiting", pct: 0, key: "", error: "" }))];
    },
    removeQueued(q) {
      this.queue = this.queue.filter((x) => x !== q);
    },
    clearFinished() {
      this.queue = this.queue.filter((q) => q.state === "waiting");
    },
    toSend() {
      return this.queue.filter((q) => ["waiting", "failed"].includes(q.state));
    },

    // One file after the other; Cancel stops the one going up and the rest.
    async sendToBucket() {
      const ctrl = new AbortController();
      this._send = ctrl;
      this.sending = true;
      let sent = 0;
      for (const q of this.toSend()) {
        if (ctrl.signal.aborted) break;
        try {
          await this.sendFile(q, ctrl.signal, this.uploadTo);
          q.state = "done";
          sent++;
        } catch (e) {
          q.state = ctrl.signal.aborted ? "cancelled" : "failed";
          q.error = ctrl.signal.aborted ? "" : e.message;
        }
      }
      this.sending = false;
      this._send = null;
      if (sent) {
        const list = this.sections.find((x) => x.key === this.uploadTo).title;
        this.notice = `${sent} file${sent > 1 ? "s" : ""} uploaded to the bucket, listed under "${list}".`;
        await this.loadFiles();
      }
    },
    cancelBucket() {
      this._send?.abort();
    },

    // folder: "clips" | "reels" (videos, by the list they go to) | "audio" (music)
    async sendFile(q, signal, folder = "clips") {
      q.state = "uploading";
      q.pct = 0;
      q.error = "";
      const start = await this.postJSON("/bucket/uploads", { filename: q.file.name, size_bytes: q.file.size, folder });
      const ref = { key: start.key, upload_id: start.upload_id };
      q.key = start.key;
      try {
        const parts = [];
        for (let offset = 0, n = 1; offset < q.file.size; offset += start.part_size, n++) {
          const r = await fetch(`/bucket/uploads/part?${new URLSearchParams({ ...ref, number: n })}`, {
            method: "PUT",
            headers: { "Content-Type": "application/octet-stream" },
            body: q.file.slice(offset, offset + start.part_size),
            signal,
          });
          const j = await r.json().catch(() => ({}));
          if (!r.ok) throw new Error(j.error?.message || `HTTP ${r.status}`);
          parts.push(j);
          q.pct = Math.round((Math.min(offset + start.part_size, q.file.size) / q.file.size) * 100);
        }
        await this.postJSON("/bucket/uploads/complete", { ...ref, parts });
      } catch (e) {
        // Drop the pieces already sent (the bucket keeps, and bills, them otherwise).
        this.postJSON("/bucket/uploads/abort", ref).catch(() => {});
        throw e;
      }
    },

    queueLabel(q) {
      return {
        waiting: "Waiting", uploading: `${q.pct}%`, done: "✓ In the bucket",
        failed: "Failed", cancelled: "Cancelled",
      }[q.state];
    },
    // kind: "videos" | "audio"
    fileLink(f, kind = "videos") {
      return `/bucket/${kind}/link?key=${encodeURIComponent(f.key)}`;
    },
    downloadLink(f, kind = "videos") {
      return `/bucket/${kind}/download?key=${encodeURIComponent(f.key)}`;
    },

    // Deleted for good (a clip's captions go with it); reels that were
    // uploaded there show "Upload to bucket" again.
    async deleteFile(f, kind = "videos") {
      if (!confirm(`Delete "${f.name}" from the bucket?\n\nFolder: ${f.folder || "(top of the bucket)"}\n\n` +
        "It is deleted from the bucket for good: it can't be undone. A copy on this computer, if any, is kept.")) return;
      this.deleting = f.key;
      try {
        await this.removeFromBucket(f, kind);
        this.notice = `"${f.name}" deleted from the bucket.`;
      } catch (e) {
        this.error = e.message;
      } finally {
        this.deleting = null;
      }
    },
    // The delete itself and the lists; the caller has asked to confirm.
    async removeFromBucket(f, kind = "videos") {
      const r = await fetch(`/bucket/${kind}?key=${encodeURIComponent(f.key)}`, { method: "DELETE" });
      if (!r.ok) {
        const j = await r.json().catch(() => ({}));
        throw new Error(j.error?.message || `HTTP ${r.status}`);
      }
      if (kind === "audio") {
        this.bucketMusic = this.bucketMusic.filter((x) => x.key !== f.key);
        await this.loadTracks();
      } else {
        this.files = this.files.filter((x) => x.key !== f.key);
        this.chosen = this.chosen.filter((k) => k !== f.key);
        window.dispatchEvent(new CustomEvent("clipforge:bucket-deleted", { detail: { key: f.key } }));
      }
    },

    // ----- Tick several videos / ZIP files in a list: download or delete them together -----
    isChosen(f) {
      return this.chosen.includes(f.key);
    },
    toggleChosen(f) {
      this.chosen = this.isChosen(f) ? this.chosen.filter((k) => k !== f.key) : [...this.chosen, f.key];
    },
    // The rows on screen in a list (search, folder and "Show more" applied).
    shownFiles(sec) {
      return this.filtered(sec).slice(0, this.shown[sec]);
    },
    allShownChosen(sec) {
      const rows = this.shownFiles(sec);
      return rows.length > 0 && rows.every((f) => this.isChosen(f));
    },
    chooseAllShown(sec, on) {
      const keys = this.shownFiles(sec).map((f) => f.key);
      this.chosen = on ? [...new Set([...this.chosen, ...keys])] : this.chosen.filter((k) => !keys.includes(k));
    },
    // The ticked files of one list: the other list's stay ticked.
    chosenFiles(sec) {
      return this.sectionFiles(sec).filter((f) => this.isChosen(f));
    },
    clearChosen(sec) {
      const keys = new Set(this.sectionFiles(sec).map((f) => f.key));
      this.chosen = this.chosen.filter((k) => !keys.has(k));
    },
    // One after the other, a second apart, each in a hidden frame: started all
    // at once, the browser drops some. Chrome asks once to allow several downloads.
    async downloadChosen(sec) {
      const list = this.chosenFiles(sec);
      if (!list.length || this.bulkJob) return;
      this.bulkJob = { section: sec, action: "download", done: 0, total: list.length };
      for (const f of list) {
        const frame = document.createElement("iframe");
        frame.hidden = true;
        frame.src = this.downloadLink(f);
        document.body.appendChild(frame);
        setTimeout(() => frame.remove(), 60_000); // the download has long started by then
        this.bulkJob.done++;
        await new Promise((ok) => setTimeout(ok, 1000));
      }
      this.bulkJob = null;
      this.notice = `${list.length} download${list.length > 1 ? "s" : ""} started: see your browser's downloads.`;
    },
    async deleteChosen(sec) {
      const list = this.chosenFiles(sec);
      if (!list.length || this.bulkJob) return;
      const names = list.slice(0, 10).map((f) => `• ${f.name}`).join("\n") +
        (list.length > 10 ? `\n… and ${list.length - 10} more` : "");
      if (!confirm(`Delete ${list.length} file${list.length > 1 ? "s" : ""} from the bucket?\n\n${names}\n\n` +
        "They are deleted from the bucket for good: it can't be undone. Copies on this computer, if any, are kept.")) return;
      this.bulkJob = { section: sec, action: "delete", done: 0, total: list.length };
      const failed = [];
      for (const f of list) {
        try {
          await this.removeFromBucket(f);
        } catch (e) {
          failed.push(`${f.name}: ${e.message}`);
        }
        this.bulkJob.done++;
      }
      this.bulkJob = null;
      const deleted = list.length - failed.length;
      if (deleted) this.notice = `${deleted} file${deleted > 1 ? "s" : ""} deleted from the bucket.`;
      if (failed.length) this.error = `Could not delete ${failed.length}: ${failed.slice(0, 3).join("; ")}`;
    },

    // ----- Rename an audio file (same folder; its extension stays) -----
    stemOf(name) {
      const i = name.lastIndexOf(".");
      return i > 0 ? name.slice(0, i) : name;
    },
    extOf(name) {
      const i = name.lastIndexOf(".");
      return i > 0 ? name.slice(i) : "";
    },
    startRename(f) {
      this.renaming = { key: f.key, name: this.stemOf(f.name), busy: false };
    },
    cancelRename() {
      this.renaming = { key: "", name: "", busy: false };
    },
    async saveRename(f) {
      const name = this.renaming.name.trim();
      if (this.renaming.busy || !name) return;
      if (name === this.stemOf(f.name)) return this.cancelRename();
      this.renaming.busy = true;
      try {
        const j = await this.postJSON("/bucket/audio/rename", { key: f.key, name });
        this.cancelRename();
        this.notice = `Renamed to "${j.name}".`;
        await Promise.all([this.loadAudio(), this.loadTracks()]);
      } catch (e) {
        this.renaming.busy = false;
        this.error = e.message;
      }
    },
    filteredAudio() {
      const text = this.audioText.trim().toLowerCase();
      return this.bucketMusic.filter((f) => !text || f.key.toLowerCase().includes(text));
    },

    // ----- Add music straight to the bucket (<prefix>/audio/) -----
    // Files from this computer go up in parts, one after the other.
    async addMusicFiles(e) {
      const files = [...e.target.files];
      e.target.value = "";
      let sent = 0;
      for (const file of files) {
        this.musicAdd.file = { file, state: "waiting", pct: 0, key: "", error: "" };
        try {
          await this.sendFile(this.musicAdd.file, undefined, "audio");
          sent++;
        } catch (err) {
          this.error = `"${file.name}" could not go to the bucket: ${err.message}`;
          break;
        }
      }
      this.musicAdd.file = null;
      if (sent) {
        this.notice = sent === 1 ? `"${files[0].name}" saved to the bucket.` : `${sent} music files saved to the bucket.`;
        await this.loadAudio();
      }
    },
    // The audio of a link: the server downloads it and sends it to the bucket
    // (its progress shows below the box, as the music lists poll it).
    async addMusicLink() {
      if (!this.musicAdd.link || this.musicAdd.grabbing) return;
      this.musicAdd.grabbing = true;
      try {
        await this.postJSON("/bucket/audio/from-url", { url: this.musicAdd.link });
        this.musicAdd.link = "";
        await this.loadTracks();
      } catch (e) {
        this.error = e.message;
      } finally {
        this.musicAdd.grabbing = false;
      }
    },
    bucketImports() {
      return this.tracks.filter((t) => t.to_bucket);
    },
    // A link import that has left the list is in the bucket now (a failed one
    // stays, saying why).
    checkBucketImports() {
      const listed = this.bucketImports().map((t) => t.id);
      const saved = this._importing.filter((id) => !listed.includes(id)).length;
      this._importing = this.bucketImports().filter((t) => t.status === "processing").map((t) => t.id);
      if (saved) {
        this.notice = saved === 1 ? "The audio of the link is saved to the bucket." : `${saved} links' audio saved to the bucket.`;
        this.loadAudio();
      }
    },

    // ----- The two lists: every video and ZIP file in the bucket, by where it came from -----
    isZip(f) {
      return f.name.toLowerCase().endsWith(".zip"); // can't be played: Download only
    },
    // [list, folder] of a file. "Create a reel" sends its reels from a topic to
    // reels/ and from a video to clips/; "Clip a video" sends nothing itself, so
    // its list is what was uploaded for it (uploads/clips/, and uploads/ from
    // before there was a choice) and any folder ClipForge doesn't use.
    place(f) {
      const s = this.status;
      const [topic, video] = s.reels_folders;
      if (f.key.startsWith(topic)) return ["reels", "topic"];
      if (f.key.startsWith(video)) return ["reels", "video"];
      if (f.key.startsWith(s.upload_folders.reels)) return ["reels", "uploads"];
      if (f.key.startsWith(s.uploads_folder)) return ["clips", "uploads"];
      return ["clips", "other"];
    },
    sectionFiles(sec) {
      return this.files.filter((f) => this.place(f)[0] === sec);
    },
    // The folder menu of a list: only the folders it has files in (one: no menu).
    areas(sec) {
      const files = this.sectionFiles(sec);
      const labels = { topic: "Reels from a topic", video: "Reels from a video", uploads: "Your uploads", other: "Other folders" };
      const found = Object.entries(labels)
        .map(([key, label]) => ({ key, label, count: files.filter((f) => this.place(f)[1] === key).length }))
        .filter((a) => a.count);
      return found.length > 1 ? [{ key: "all", label: "All files", count: files.length }, ...found] : [];
    },
    filtered(sec) {
      const text = this.filters[sec].text.trim().toLowerCase();
      // A folder whose files are all deleted leaves the menu: the whole list again.
      const area = this.areas(sec).some((a) => a.key === this.filters[sec].area) ? this.filters[sec].area : "all";
      return this.sectionFiles(sec).filter((f) =>
        (area === "all" || this.place(f)[1] === area) &&
        (!text || f.key.toLowerCase().includes(text)));
    },
  };
}
