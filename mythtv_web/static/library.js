let programGroups = [];
let currentPrograms = [];
let selectedGroupTitle = null;
let selectedGroupArtwork = null;
let selectedGroupArtworkType = null;
let recordingGroupsScrollY = 0;
let totalRecordings = 0;
let activeFilter = "all";
let videos = [];
let activeVideoFilter = "all";
let activeVideoMode = "all";
let videoDirectoryPath = [];
const videoDirectoryScrollPositions = new Map();
let activeVideo = null;
let loading = false;

const grid = document.querySelector("#program-grid");
const appRoot = document.querySelector('meta[name="app-root"]')?.content.replace(/\/$/, "") || "";
const CAN_DELETE = document.body.dataset.userRole !== "guest";
const searchInput = document.querySelector("#search");
const emptyState = document.querySelector("#empty-state");
const syncNote = document.querySelector("#sync-note");
const resultNote = document.querySelector("#result-note");
const backButton = document.querySelector("#back-to-groups");
const playerDialog = document.querySelector("#player-dialog");
const playerVideo = document.querySelector("#recording-video");
const playerShell = document.querySelector(".player-shell");
const playerOverlay = document.querySelector("#player-overlay");
const playerTheaterButton = document.querySelector("#player-theater");
const playerExitTheaterButton = document.querySelector("#player-exit-theater");
const playerDetachButton = document.querySelector("#player-detach");
const playerVolume = document.querySelector("#player-volume");
const playerMute = document.querySelector("#player-mute");
let lastNonzeroVolume = 1;
const deleteDialog = document.querySelector("#delete-dialog");
const deleteCheckbox = document.querySelector("#delete-confirm-check");
const deleteButton = document.querySelector("#delete-confirm-button");
const deleteCancelButton = document.querySelector("#delete-dialog-cancel");
const deleteStatus = document.querySelector("#delete-status");
const playerStatus = document.querySelector("#player-status");
const playbackSeek = document.querySelector("#playback-seek");
const smartSkip = document.querySelector("#smart-skip");
const skipAdButton = document.querySelector("#skip-ad");
const jumpForwardButton = document.querySelector("#jump-forward");
const setBookmarkButton = document.querySelector("#set-bookmark");
let activeRecording = null;
let videoPlaybackInfo = null;
let playbackInfo = null;
let playerReturnFocus = null;
let pendingDelete = null;
let deleteInProgress = false;
let commercialBreaks = [];
let usingTranscode = false;
let usingWasm = false;
let transcodeBase = 0;
let transcodeToken = null;
let wasmPlayback = null;
let subtitleElement = null;
let lastSavedPosition = -1;
let requestedStartPosition = 0;
let smartSkipBusy = false;
let controlsHideTimer = null;
let forwardSkipTimer = null;
let forwardSkipMode = false;
let liveRecordingTimer = null;
let playerController = null;
let playerGeneration = 0;
let playbackReady = false;
const BROWSER_CONVERSION_ENABLED = false;

function waitForPlayerEvent(eventName, signal, timeout = 30_000) {
  return new Promise((resolve) => {
    let timer;
    const finish = (ready) => {
      window.clearTimeout(timer);
      playerVideo.removeEventListener(eventName, onReady);
      playerVideo.removeEventListener("error", onError);
      signal.removeEventListener("abort", onError);
      resolve(ready);
    };
    const onReady = () => finish(true);
    const onError = () => finish(false);
    if (signal.aborted) return finish(false);
    playerVideo.addEventListener(eventName, onReady, { once: true });
    playerVideo.addEventListener("error", onError, { once: true });
    signal.addEventListener("abort", onError, { once: true });
    timer = window.setTimeout(onError, timeout);
  });
}

function appPath(path) {
  return `${appRoot}${path}`;
}

function appLocalPath(path) {
  return appRoot && (path === appRoot || path.startsWith(`${appRoot}/`))
    ? path
    : appPath(path);
}

function formatDate(value) {
  if (!value) return "Date unknown";
  const date = new Date(value);
  if (Number.isNaN(date.getTime()) || date.getUTCFullYear() < 1980) {
    return "Date unknown";
  }
  return new Intl.DateTimeFormat(undefined, {
    day: "numeric",
    month: "short",
    year: "numeric",
    timeZone: "UTC",
  }).format(date);
}

function formatSize(bytes) {
  if (!bytes) return "";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let size = bytes;
  let unitIndex = 0;
  while (size >= 1024 && unitIndex < units.length - 1) {
    size /= 1024;
    unitIndex += 1;
  }
  return `${size.toFixed(unitIndex === 0 ? 0 : 1)} ${units[unitIndex]}`;
}

function formatPlaybackTime(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return "0:00";
  const wholeSeconds = Math.floor(seconds);
  const hours = Math.floor(wholeSeconds / 3600);
  const minutes = Math.floor((wholeSeconds % 3600) / 60);
  const remainder = wholeSeconds % 60;
  return hours
    ? `${hours}:${String(minutes).padStart(2, "0")}:${String(remainder).padStart(2, "0")}`
    : `${minutes}:${String(remainder).padStart(2, "0")}`;
}

function playbackDuration(program) {
  const start = Date.parse(program.StartTime || "");
  const end = Date.parse(program.EndTime || "");
  return Number.isFinite(start) && Number.isFinite(end) && end > start
    ? (end - start) / 1000
    : 0;
}

function playbackLength() {
  const recordingDuration = playbackDuration(activeRecording || {});
  if (recordingDuration > 0) return recordingDuration;
  const videoDuration = Number(videoPlaybackInfo?.duration);
  if (Number.isFinite(videoDuration) && videoDuration > 0) return videoDuration;
  return Number.isFinite(playerVideo.duration) && playerVideo.duration > 0
    ? playerVideo.duration
    : 0;
}

function playbackPosition() {
  return usingTranscode || usingWasm
    ? transcodeBase + playerVideo.currentTime
    : playerVideo.currentTime;
}

function seekTo(seconds) {
  const duration = playbackLength();
  const position = Math.max(0, Math.min(seconds, duration || Number.MAX_SAFE_INTEGER));
  if (usingWasm) {
    const buffered = Array.from({ length: playerVideo.buffered.length }, (_, index) => ({
      start: playerVideo.buffered.start(index),
      end: playerVideo.buffered.end(index),
    })).some((range) => position >= range.start && position <= range.end);
    if (buffered) playerVideo.currentTime = position - transcodeBase;
    else startTranscode(position);
  } else if (usingTranscode) {
    startTranscode(position);
  } else {
    playerVideo.currentTime = position;
  }
}

function secondsFromMarker(value) {
  const numeric = Number(value);
  if (Number.isFinite(numeric)) return numeric / 1000;
  const parts = String(value || "").split(":").map(Number);
  if (parts.some((part) => !Number.isFinite(part))) return NaN;
  return parts.reduce((total, part) => total * 60 + part, 0);
}

function setForwardSkipMode(enabled) {
  window.clearTimeout(forwardSkipTimer);
  forwardSkipMode = enabled;
  const skipLabel = jumpForwardButton.querySelector(".player-icon-label");
  const seconds = enabled ? 5 : 30;
  skipLabel.textContent = `${seconds}s`;
  jumpForwardButton.setAttribute("aria-label", `Jump forward ${seconds} seconds`);
  jumpForwardButton.title = enabled
    ? "Smart skip: jump forward 5 seconds"
    : "Jump forward 30 seconds";
  jumpForwardButton.classList.toggle("player-forward-smart", enabled);
  if (enabled) {
    forwardSkipTimer = window.setTimeout(() => setForwardSkipMode(false), 5000);
  }
}

function updateCommercialBreaks(markers) {
  commercialBreaks = (markers || []).map((marker) => ({
    start: secondsFromMarker(marker.Start ?? marker.start),
    end: secondsFromMarker(marker.End ?? marker.end),
  })).filter((marker) => Number.isFinite(marker.start) && Number.isFinite(marker.end) && marker.end > marker.start);
  skipAdButton.hidden = commercialBreaks.length === 0;
  skipAdButton.disabled = commercialBreaks.length === 0;
}

async function refreshLiveRecording(recordedId) {
  if (activeRecording?.Recording?.RecordedId !== recordedId) return;
  const controller = playerController;
  try {
    const response = await fetch(appPath(`/api/playback/${recordedId}/live-status`), {
      signal: controller.signal,
    });
    if (!response.ok) throw new Error(`Request failed (${response.status})`);
    const status = await response.json();
    if (controller !== playerController || controller.signal.aborted) return;
    if (status.end_time) {
      activeRecording.EndTime = status.end_time;
      activeRecording.Recording.EndTs = status.end_time;
      updatePlaybackProgress();
    }
    if (status.in_progress) updateCommercialBreaks(status.comm_breaks);
    else {
      window.clearInterval(liveRecordingTimer);
      liveRecordingTimer = null;
    }
  } catch (error) {
    if (controller === playerController && !controller.signal.aborted) {
      playerStatus.textContent = `Could not refresh live recording status: ${error.message}`;
    }
  }
}

function startLiveRecordingUpdates(recordedId) {
  window.clearInterval(liveRecordingTimer);
  refreshLiveRecording(recordedId);
  liveRecordingTimer = window.setInterval(
    () => refreshLiveRecording(recordedId),
    20_000,
  );
}

function updatePlaybackProgress() {
  if ((!activeRecording && !activeVideo) || (activeRecording && !playbackReady)) return;
  const position = playbackPosition();
  const duration = playbackLength();
  document.querySelector("#playback-elapsed").textContent = formatPlaybackTime(position);
  document.querySelector("#playback-duration").textContent = formatPlaybackTime(duration);
  playbackSeek.value = duration > 0 ? String(Math.round((position / duration) * 1000)) : "0";
  if (duration > 0 && Math.abs(position - lastSavedPosition) >= 15) {
    savePlaybackPosition();
  }
}

async function savePlaybackPosition() {
  if ((!activeRecording && !activeVideo) || (activeRecording && !playbackReady)) return;
  const generation = playerGeneration;
  const recordedId = activeRecording?.Recording.RecordedId;
  const videoId = activeVideo?.Id;
  const position = playbackPosition();
  if (!Number.isFinite(position) || position < 0) return;
  lastSavedPosition = position;
  try {
    const positionPath = videoId
      ? `/api/videos/${videoId}/position`
      : `/api/playback/${recordedId}/position`;
    const response = await fetch(appPath(positionPath), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ position }),
      keepalive: true,
    });
    if (!response.ok) throw new Error(`Request failed (${response.status})`);
  } catch (error) {
  if (
    generation === playerGeneration
    && (activeRecording?.Recording.RecordedId === recordedId || activeVideo?.Id === videoId)
  ) {
      lastSavedPosition = -1;
      playerStatus.textContent = `Could not save playback position: ${error.message}`;
    }
  }
}

async function cancelTranscode() {
  const token = transcodeToken;
  if (!token || (!activeRecording && !activeVideo)) return;
  transcodeToken = null;
  const cancelUrl = activeVideo
    ? `/api/videos/${activeVideo.Id}/transcode/cancel`
    : `/api/playback/${activeRecording.Recording.RecordedId}/transcode/cancel`;
  for (let attempt = 0; attempt < 4; attempt += 1) {
    try {
      const response = await fetch(
        appPath(cancelUrl),
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ token }),
          keepalive: true,
        },
      );
      if (response.ok || response.status !== 404) return;
    } catch {
      return;
    }
    await new Promise((resolve) => window.setTimeout(resolve, 100));
  }
}

function stopWasmPlayback() {
  const playback = wasmPlayback;
  wasmPlayback = null;
  usingWasm = false;
  if (playback) playback.cancel();
}

function createTranscodeToken() {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
}

function startTranscode(seconds = 0) {
  if (
    (!activeRecording && !activeVideo)
    || !playerController
    || playerController.signal.aborted
  ) return;
  stopWasmPlayback();
  cancelTranscode();
  usingWasm = false;
  usingTranscode = true;
  transcodeBase = seconds;
  const playbackRate = Number(document.querySelector("#playback-speed").value) || 1;
  transcodeToken = createTranscodeToken();
  const token = transcodeToken;
  const generation = playerGeneration;
  playbackReady = false;
  playerStatus.textContent = "Preparing a live browser-compatible stream…";
  waitForPlayerEvent("loadedmetadata", playerController.signal).then((ready) => {
    if (!ready || generation !== playerGeneration || token !== transcodeToken) return;
    playbackReady = true;
    playerVideo.playbackRate = playbackRate;
    if ("preservesPitch" in playerVideo) playerVideo.preservesPitch = true;
  });
  const transcodePath = activeVideo
    ? `/api/videos/${activeVideo.Id}/transcode`
    : `/api/playback/${activeRecording.Recording.RecordedId}/transcode`;
  playerVideo.src = appPath(`${transcodePath}?start=${encodeURIComponent(seconds)}&token=${transcodeToken}`);
  playerVideo.load();
  if (subtitleElement) {
    selectSubtitle(document.querySelector("#subtitle-track").value);
  }
  playerVideo.play().catch(() => {
    if (generation === playerGeneration && token === transcodeToken) {
      playerStatus.textContent = "Stream ready. Press Play to begin.";
    }
  });
}

function handleVideoError() {
  if (activeVideo) {
    if (!usingTranscode) {
      const position = playbackPosition();
      startTranscode(Math.max(Number.isFinite(position) ? position : 0, requestedStartPosition));
    } else {
      playerStatus.textContent = "The server could not transcode this video.";
    }
    return;
  }
  if (!activeRecording) return;
  if (!usingTranscode) {
    if (BROWSER_CONVERSION_ENABLED && !usingWasm) {
      startWasmTranscode(Math.max(playbackPosition(), requestedStartPosition));
    } else {
      startTranscode(Math.max(playbackPosition(), requestedStartPosition));
    }
  } else {
    playerStatus.textContent = "The server could not transcode this recording.";
  }
}

async function selectSubtitle(streamIndex) {
  subtitleElement?.remove();
  subtitleElement = null;
  if (streamIndex === "" || (!activeRecording && !activeVideo)) return;
  const tracks = activeVideo
    ? videoPlaybackInfo?.subtitles || []
    : playbackInfo?.media.subtitles || [];
  const trackInfo = tracks.find(
    (track) => String(track.index) === streamIndex,
  );
  if (!trackInfo) return;
  const track = document.createElement("track");
  const generation = playerGeneration;
  track.kind = "subtitles";
  track.label = trackInfo.title || (
    trackInfo.language && trackInfo.language !== "und"
      ? trackInfo.language
      : `Track ${streamIndex}`
  );
  track.srclang = /^[a-z]{2,3}$/i.test(trackInfo.language) ? trackInfo.language : "und";
  const subtitlePath = activeVideo
    ? `/api/videos/${activeVideo.Id}/subtitles/${streamIndex}`
    : `/api/playback/${activeRecording.Recording.RecordedId}/subtitles/${streamIndex}`;
  const subtitleStart = usingTranscode || usingWasm ? transcodeBase : 0;
  track.src = appPath(`${subtitlePath}?start=${encodeURIComponent(subtitleStart)}`);
  track.addEventListener("load", () => {
    if (generation !== playerGeneration || subtitleElement !== track) return;
    track.track.mode = "showing";
    playerStatus.textContent = `Subtitles: ${track.label}`;
  });
  track.addEventListener("error", () => {
    if (generation !== playerGeneration || subtitleElement !== track) return;
    playerStatus.textContent = trackInfo.codec === "dvb_subtitle"
      ? "This DVB bitmap subtitle could not be converted to browser text subtitles."
      : `Could not load subtitle track: ${track.label}`;
  });
  playerVideo.append(track);
  subtitleElement = track;
  track.track.mode = "showing";
}

async function startWasmTranscode(seconds = 0) {
  if (!activeRecording) return;
  const generation = playerGeneration;
  stopWasmPlayback();
  cancelTranscode();
  usingTranscode = false;
  usingWasm = true;
  transcodeBase = seconds;
  const recording = activeRecording;
  playerStatus.textContent = "Preparing browser-side conversion…";
  try {
    const { startWasmPlayback } = await import("./browser_playback.js");
    if (generation !== playerGeneration || recording !== activeRecording) return;
    const session = await startWasmPlayback({
      sourceURL: appPath(`/api/playback/${recording.Recording.RecordedId}/stream`),
      coreURL: appPath("/static/vendor/ffmpeg-core.js"),
      wasmURL: appPath("/static/vendor/ffmpeg-core.wasm"),
      workerURL: appPath("/static/vendor/worker.js"),
      video: playerVideo,
      startSeconds: seconds,
      sourceVideoCodec: playbackInfo.media.video_codec,
      sourceVideoProfile: playbackInfo.media.video_profile,
      sourceVideoLevel: playbackInfo.media.video_level,
      sourceVideoPixelFormat: playbackInfo.media.video_pixel_format,
      onStatus: (status) => {
        if (generation === playerGeneration && recording === activeRecording) playerStatus.textContent = status;
      },
    });
    if (generation !== playerGeneration || recording !== activeRecording) {
      session.cancel();
      return;
    }
    wasmPlayback = session;
    playbackReady = true;
    if (subtitleElement) {
      selectSubtitle(document.querySelector("#subtitle-track").value);
    }
    session.done.catch((error) => {
      if (wasmPlayback !== session || recording !== activeRecording) return;
      const position = playbackPosition();
      playerStatus.textContent = `Browser conversion unavailable; switching to server fallback: ${error.message}`;
      startTranscode(position);
    });
    playerVideo.play().catch(() => {
      if (generation === playerGeneration && recording === activeRecording) {
        playerStatus.textContent = "Browser conversion ready. Press Play to begin.";
      }
    });
  } catch (error) {
    if (generation === playerGeneration && recording === activeRecording) {
      playerStatus.textContent = `Browser conversion unavailable; using server fallback: ${error.message}`;
      startTranscode(seconds);
    }
  }
}

async function openPlayer(program, startMode = "resume") {
  const recordedId = program.Recording?.RecordedId;
  if (!recordedId) return;
  cancelTranscode();
  playerController?.abort();
  const controller = new AbortController();
  playerController = controller;
  const generation = ++playerGeneration;
  const isCurrent = () => generation === playerGeneration && !controller.signal.aborted;
  playbackReady = false;
  activeRecording = program;
  activeVideo = null;
  videoPlaybackInfo = null;
  playerVideo.pause();
  playerVideo.removeAttribute("src");
  playerVideo.load();
  subtitleElement?.remove();
  subtitleElement = null;
  window.clearInterval(liveRecordingTimer);
  liveRecordingTimer = null;
  setForwardSkipMode(false);
  playbackInfo = null;
  commercialBreaks = [];
  skipAdButton.hidden = true;
  skipAdButton.disabled = true;
  document.querySelector(".smart-skip-control").hidden = false;
  setBookmarkButton.disabled = false;
  usingTranscode = false;
  usingWasm = false;
  transcodeBase = 0;
  stopWasmPlayback();
  lastSavedPosition = -1;
  requestedStartPosition = 0;
  document.querySelector("#player-title").textContent = program.SubTitle || program.Title;
  document.querySelector("#player-subtitle").textContent = [
    program.Title,
    episodeLabel(program),
    formatDate(program.StartTime),
  ].filter(Boolean).join(" · ");
  document.querySelector("#player-description").textContent =
    program.Description || "No program description is available.";
  document.querySelector("#subtitle-track").replaceChildren(new Option("Off", ""));
  document.querySelector("#player-details").open = false;
  playerStatus.textContent = "Loading MythTV playback details…";
  playerReturnFocus = document.activeElement;
  if (!playerDialog.open) playerDialog.showModal();
  playerVideo.focus({ preventScroll: true });

  try {
    const response = await fetch(appPath(`/api/playback/${recordedId}`), {
      signal: controller.signal,
    });
    if (!response.ok) throw new Error(`Request failed (${response.status})`);
    const info = await response.json();
    if (!isCurrent()) return;
    playbackInfo = info;
    if (playbackInfo.program) {
      activeRecording = {
        ...program,
        ...playbackInfo.program,
        Recording: { ...program.Recording, ...playbackInfo.program.Recording },
      };
    }
    updateCommercialBreaks(playbackInfo.comm_breaks);
    if (activeRecording.Recording?.StatusName?.toLowerCase() === "recording") {
      startLiveRecordingUpdates(recordedId);
    }
    const subtitles = document.querySelector("#subtitle-track");
    playbackInfo.media.subtitles.forEach((track) => {
      const label = track.title || (
        track.language && track.language !== "und"
          ? track.language
          : `Track ${track.index}`
      );
      const option = new Option(
        label,
        String(track.index),
      );
      subtitles.add(option);
    });
    const resumeMilliseconds = startMode === "beginning"
      ? 0
      : startMode === "bookmark"
        ? playbackInfo.bookmark
        : playbackInfo.last_position;
    const resumePosition = Math.max(0, resumeMilliseconds / 1000);
    requestedStartPosition = resumePosition;
    const knownUnsupported = ["hevc", "eac3", "mpeg2video", "mpeg1video", "mp2", "ac3"].some((codec) =>
      [playbackInfo.media.video_codec, playbackInfo.media.audio_codec].includes(codec),
    );
    if (knownUnsupported) {
      lastSavedPosition = resumePosition;
      if (BROWSER_CONVERSION_ENABLED) startWasmTranscode(resumePosition);
      else startTranscode(resumePosition);
    } else {
      const metadataReady = waitForPlayerEvent("loadedmetadata", controller.signal);
      playerVideo.src = appPath(`/api/playback/${recordedId}/stream`);
      playerVideo.load();
      const ready = await metadataReady;
      if (!isCurrent() || usingTranscode || usingWasm) return;
      if (!ready) throw new Error("Timed out loading recording metadata.");
      const duration = Number.isFinite(playerVideo.duration) ? playerVideo.duration : 0;
      const seekPosition = duration > 0
        ? Math.min(resumePosition, Math.max(0, duration - 0.5))
        : resumePosition;
      lastSavedPosition = seekPosition;
      if (seekPosition > 0.5) {
        const seeked = waitForPlayerEvent("seeked", controller.signal, 5000);
        playerVideo.currentTime = seekPosition;
        await seeked;
        if (!isCurrent() || usingTranscode || usingWasm) return;
      }
      playbackReady = true;
      updatePlaybackProgress();
      await playerVideo.play().then(() => {
        if (!isCurrent()) return;
        requestedStartPosition = 0;
        playerStatus.textContent = commercialBreaks.length
          ? `${commercialBreaks.length} commercial break markers loaded`
          : "No commercial break markers are available for this recording.";
      }).catch(() => {
        if (isCurrent()) playerStatus.textContent = "Stream ready. Press Play to begin.";
      });
    }
  } catch (error) {
    if (isCurrent()) playerStatus.textContent = error.message;
  }
}

async function openVideoPlayer(video) {
  cancelTranscode();
  playerController?.abort();
  const controller = new AbortController();
  playerController = controller;
  const generation = ++playerGeneration;
  const isCurrent = () => generation === playerGeneration && !controller.signal.aborted;
  playbackReady = false;
  activeVideo = video;
  activeRecording = null;
  playbackInfo = null;
  videoPlaybackInfo = null;
  playerVideo.pause();
  playerVideo.removeAttribute("src");
  playerVideo.load();
  subtitleElement?.remove();
  subtitleElement = null;
  window.clearInterval(liveRecordingTimer);
  liveRecordingTimer = null;
  setForwardSkipMode(false);
  playbackInfo = null;
  commercialBreaks = [];
  skipAdButton.hidden = true;
  skipAdButton.disabled = true;
  document.querySelector(".smart-skip-control").hidden = true;
  usingTranscode = false;
  usingWasm = false;
  transcodeBase = 0;
  requestedStartPosition = 0;
  stopWasmPlayback();
  playerVideo.controls = false;
  playerShell.classList.add("video-playback");
  document.querySelector("#player-title").textContent = video.Title || "Video";
  document.querySelector("#player-subtitle").textContent = [
    video.SubTitle,
    video.ReleaseDate ? formatDate(video.ReleaseDate) : "",
    video.Length ? `${video.Length} min` : "",
  ].filter(Boolean).join(" · ");
  document.querySelector("#player-description").textContent =
    video.Description || "No video description is available.";
  document.querySelector("#subtitle-track").replaceChildren(new Option("Off", ""));
  document.querySelector("#player-details summary").textContent = "Video details";
  document.querySelector("#player-details").open = false;
  playerStatus.textContent = "Loading MythTV video details…";
  playerReturnFocus = document.activeElement;
  playerDialog.showModal();
  playerVideo.focus({ preventScroll: true });

  try {
    const response = await fetch(appPath(`/api/videos/${video.Id}/playback`), {
      signal: controller.signal,
    });
    if (!response.ok) throw new Error(`Request failed (${response.status})`);
    videoPlaybackInfo = await response.json();
  } catch (error) {
    playerStatus.textContent = `Could not load video playback details: ${error.message}`;
    videoPlaybackInfo = { duration: null, frame_rate: null, last_position: 0, bookmark: 0, subtitles: [] };
  }
  if (!isCurrent()) return;
  const subtitleSelect = document.querySelector("#subtitle-track");
  videoPlaybackInfo.subtitles.forEach((track) => {
    const label = track.title || (
      track.language && track.language !== "und" ? track.language : `Track ${track.index}`
    );
    subtitleSelect.add(new Option(label, String(track.index)));
  });
  const duration = Number(videoPlaybackInfo.duration);
  const resumePosition = Number(videoPlaybackInfo.last_position) || 0;
  requestedStartPosition = resumePosition;
  lastSavedPosition = resumePosition;
  document.querySelector("#playback-duration").textContent = formatPlaybackTime(
    duration > 0 ? duration : 0,
  );
  setBookmarkButton.disabled = !videoPlaybackInfo.frame_rate;
  playerVideo.addEventListener("loadedmetadata", function resumeVideo() {
    if (!isCurrent()) return;
    const mediaDuration = Number.isFinite(playerVideo.duration) ? playerVideo.duration : duration;
    const seekPosition = mediaDuration > 0
      ? Math.min(resumePosition, Math.max(0, mediaDuration - 0.5))
      : resumePosition;
    if (seekPosition > 0.5) playerVideo.currentTime = seekPosition;
    requestedStartPosition = 0;
    updatePlaybackProgress();
  }, { once: true });
  playerVideo.src = appPath(`/api/videos/${video.Id}/stream`);
  playerVideo.load();
  playerVideo.play().catch(() => {
    playerStatus.textContent = "Video ready. Press Play to begin.";
  });
}

function closePlayer() {
  if (document.fullscreenElement === playerShell) {
    document.exitFullscreen().catch((error) => {
      playerStatus.textContent = `Could not exit fullscreen: ${error.message}`;
    });
  }
  if (!activeRecording && !activeVideo) return;
  setTheaterMode(false);
  if (document.pictureInPictureElement === playerVideo && document.exitPictureInPicture) {
    document.exitPictureInPicture().catch((error) => {
      playerStatus.textContent = `Could not reattach player: ${error.message}`;
    });
  }
  savePlaybackPosition();
  playerController?.abort();
  playerController = null;
  playerGeneration += 1;
  playbackReady = false;
  window.clearInterval(liveRecordingTimer);
  liveRecordingTimer = null;
  setForwardSkipMode(false);
  cancelTranscode();
  stopWasmPlayback();
  playerVideo.pause();
  playerVideo.removeAttribute("src");
  playerVideo.load();
  subtitleElement?.remove();
  subtitleElement = null;
  activeRecording = null;
  activeVideo = null;
  playbackInfo = null;
  videoPlaybackInfo = null;
  commercialBreaks = [];
  usingTranscode = false;
  usingWasm = false;
  transcodeBase = 0;
  requestedStartPosition = 0;
  if (playerDialog.open) playerDialog.close();
  playerVideo.controls = false;
  document.querySelector(".smart-skip-control").hidden = false;
  setBookmarkButton.disabled = false;
  document.querySelector("#player-details summary").textContent = "Program details";
  playerShell.classList.remove("video-playback");
  const returnFocus = playerReturnFocus;
  playerReturnFocus = null;
  if (returnFocus?.isConnected) returnFocus.focus({ preventScroll: true });
}

function episodeLabel(program) {
  const season = Number(program.Season);
  const episode = Number(program.Episode);
  if (season > 0 && episode > 0) {
    return `S${String(season).padStart(2, "0")} E${String(episode).padStart(2, "0")}`;
  }
  const match = (program.SubTitle || "").match(/\bS(\d{1,2})E(\d{1,2})\b/i);
  return match ? `S${match[1].padStart(2, "0")} E${match[2].padStart(2, "0")}` : "";
}

function isWatched(program) {
  return (program.ProgramFlagNames || "").split("|").includes("WATCHED");
}

function artworkInfo(program, preferredType = "fanart") {
  const artworkInfos = program.Artwork?.ArtworkInfos || [];
  const fallbackType = preferredType === "fanart" ? "coverart" : "fanart";
  return (
    artworkInfos.find((item) => item.Type === preferredType && item.URL) ||
    artworkInfos.find((item) => item.Type === fallbackType && item.URL)
  );
}

function artworkUrl(program, preferredType = "fanart") {
  const artwork = artworkInfo(program, preferredType);
  return artwork?.URL
    ? appPath(`/api/artwork?url=${encodeURIComponent(artwork.URL)}`)
    : "";
}

function previewUrl(program) {
  const recordedId = program.Recording?.RecordedId;
  return Number.isInteger(recordedId) && recordedId > 0
    ? appPath(`/api/preview?recorded_id=${recordedId}`)
    : "";
}

function makeVideoCard(video, index) {
  const card = document.createElement("article");
  card.className = "program-card";
  card.style.animationDelay = `${Math.min(index % 8, 8) * 22}ms`;
  const artwork = artworkInfo(video, "coverart");
  const poster = makePoster(
    artwork?.URL || "",
    video.Watched ? "Watched" : "",
    `/api/video-preview/${video.Id}`,
    artwork?.Type || "coverart",
  );
  poster.classList.add("video-poster");
  const playButton = document.createElement("button");
  playButton.className = "recording-poster-button";
  playButton.type = "button";
  playButton.setAttribute("aria-label", `Play ${video.Title || "video"}`);
  playButton.title = "Play video";
  playButton.append(poster);
  const playIcon = document.createElement("span");
  playIcon.className = "poster-play-mark";
  playIcon.setAttribute("aria-hidden", "true");
  playIcon.innerHTML = '<svg viewBox="0 0 24 24"><path d="m8 5 12 7-12 7z" /></svg>';
  playButton.append(playIcon);
  playButton.addEventListener("click", () => openVideoPlayer(video));

  const info = document.createElement("div");
  info.className = "program-info program-info-overlay";
  const category = document.createElement("p");
  category.className = "program-channel";
  category.textContent = video.ContentType || "Video";
  const title = document.createElement("h2");
  title.className = "program-title";
  title.textContent = video.Title || "Untitled video";
  const subtitle = document.createElement("p");
  subtitle.className = "program-subtitle";
  subtitle.textContent = [episodeLabel(video), video.SubTitle || video.Director]
    .filter(Boolean)
    .join(" · ") || " ";
  const description = document.createElement("p");
  description.className = "program-description";
  description.textContent = video.Description || "";
  const meta = document.createElement("div");
  meta.className = "program-meta";
  if (video.ReleaseDate) {
    const releaseDate = document.createElement("time");
    releaseDate.dateTime = video.ReleaseDate;
    releaseDate.textContent = formatDate(video.ReleaseDate);
    meta.append(releaseDate);
  }
  if (video.Length) meta.append(document.createTextNode(`${video.Length} min`));
  info.append(category, title, subtitle, description, meta);
  poster.append(info);

  const actions = document.createElement("div");
  actions.className = "recording-actions";
  actions.setAttribute("role", "group");
  actions.setAttribute("aria-label", "Video actions");
  actions.append(makeRecordingAction(
    video.Watched ? "Mark unwatched" : "Mark watched",
    video.Watched
      ? ["M4 12a8 8 0 1 0 2.34-5.66L4 8.5", "M4 4v4.5h4.5"]
      : ["m5 12 4 4L19 6"],
    () => updateVideoWatched(video),
  ));
  card.append(playButton, actions);
  return card;
}

function videoMatchesFilters(video, query) {
  const watched = Boolean(video.Watched);
  const matchesFilter = activeVideoFilter === "all"
    || (activeVideoFilter === "watched" && watched)
    || (activeVideoFilter === "unwatched" && !watched);
  const searchable = [
    video.Title,
    video.SubTitle,
    video.Description,
    video.Director,
    video.ContentType,
    video.FileName,
    episodeLabel(video),
  ].filter(Boolean).join(" ").toLocaleLowerCase();
  return matchesFilter && (!query || searchable.includes(query));
}

function makeVideoDirectoryTree() {
  const root = { name: "", path: [], children: new Map(), videos: [] };
  for (const video of videos) {
    const segments = String(video.FileName || "")
      .replaceAll("\\", "/")
      .split("/")
      .filter(Boolean);
    segments.pop();
    let directory = root;
    for (const segment of segments) {
      if (!directory.children.has(segment)) {
        directory.children.set(segment, {
          name: segment,
          path: [...directory.path, segment],
          children: new Map(),
          videos: [],
        });
      }
      directory = directory.children.get(segment);
    }
    directory.videos.push(video);
  }
  return root;
}

function summarizeVideoDirectory(directory, query) {
  directory.visibleVideos = directory.videos.filter((video) =>
    videoMatchesFilters(video, query),
  );
  directory.matchingVideos = directory.visibleVideos.length;
  directory.matchingDirectories = 0;
  for (const child of directory.children.values()) {
    summarizeVideoDirectory(child, query);
    if (child.matchingVideos > 0) {
      directory.matchingVideos += child.matchingVideos;
      directory.matchingDirectories += 1;
    }
  }
}

function findVideoDirectory(root, path) {
  return path.reduce((directory, segment) => directory?.children.get(segment), root);
}

function makeVideoFolderCard(directory, index) {
  const card = document.createElement("article");
  card.className = "program-card video-folder-card";
  card.style.animationDelay = `${Math.min(index % 8, 8) * 22}ms`;
  const button = document.createElement("button");
  button.className = "video-folder-button";
  button.type = "button";
  button.setAttribute("aria-label", `Open folder ${directory.name}, ${directory.matchingVideos} videos`);
  button.innerHTML = '<svg class="video-folder-icon" viewBox="0 0 32 26" aria-hidden="true"><path d="M2 5.5A2.5 2.5 0 0 1 4.5 3h7l3 3h13A2.5 2.5 0 0 1 30 8.5v13a2.5 2.5 0 0 1-2.5 2.5h-21A4.5 4.5 0 0 1 2 19.5z" /></svg>';
  const name = document.createElement("h2");
  name.className = "video-folder-name";
  name.textContent = directory.name;
  const parent = document.createElement("p");
  parent.className = "video-folder-parent";
  parent.textContent = directory.path.length > 1
    ? directory.path.slice(0, -1).join(" / ")
    : "Top-level folder";
  const counts = document.createElement("p");
  counts.className = "video-folder-counts";
  const folderLabel = `${directory.matchingDirectories} ${directory.matchingDirectories === 1 ? "subfolder" : "subfolders"}`;
  const videoLabel = `${directory.matchingVideos} ${directory.matchingVideos === 1 ? "video" : "videos"}`;
  counts.textContent = `${folderLabel} · ${videoLabel}`;
  button.append(name, parent, counts);
  button.addEventListener("click", () => {
    videoDirectoryScrollPositions.set(
      JSON.stringify(videoDirectoryPath),
      window.scrollY,
    );
    videoDirectoryPath = directory.path;
    renderVideos();
    window.scrollTo({ top: 0, behavior: "instant" });
  });
  card.append(button);
  return card;
}

function renderVideoBreadcrumb() {
  const breadcrumb = document.querySelector("#video-breadcrumb");
  breadcrumb.replaceChildren();
  breadcrumb.hidden = activeVideoMode !== "folders";
  if (breadcrumb.hidden) return;

  const crumbs = [{ label: "All videos", path: [] }];
  videoDirectoryPath.forEach((segment, index) => {
    crumbs.push({ label: segment, path: videoDirectoryPath.slice(0, index + 1) });
  });
  crumbs.forEach((crumb, index) => {
    if (index > 0) {
      const separator = document.createElement("span");
      separator.textContent = "/";
      separator.setAttribute("aria-hidden", "true");
      breadcrumb.append(separator);
    }
    const button = document.createElement("button");
    button.className = "video-crumb";
    button.type = "button";
    button.textContent = crumb.label;
    if (index === crumbs.length - 1) button.setAttribute("aria-current", "page");
    button.addEventListener("click", () => {
      videoDirectoryPath = crumb.path;
      renderVideos();
      window.scrollTo({
        top: videoDirectoryScrollPositions.get(JSON.stringify(crumb.path)) || 0,
        behavior: "instant",
      });
    });
    breadcrumb.append(button);
  });
}

function renderVideos() {
  const query = document.querySelector("#video-search").value.trim().toLocaleLowerCase();
  const directoryRoot = makeVideoDirectoryTree();
  summarizeVideoDirectory(directoryRoot, query);
  if (!findVideoDirectory(directoryRoot, videoDirectoryPath)) videoDirectoryPath = [];
  const currentDirectory = findVideoDirectory(directoryRoot, videoDirectoryPath);
  const visibleVideos = activeVideoMode === "folders"
    ? currentDirectory.visibleVideos
    : videos.filter((video) => videoMatchesFilters(video, query));
  const visibleDirectories = activeVideoMode === "folders"
    ? [...currentDirectory.children.values()]
      .filter((directory) => directory.matchingVideos > 0)
      .sort((left, right) => left.name.localeCompare(
        right.name,
        undefined,
        { numeric: true, sensitivity: "base" },
      ))
    : [];
  const videoGrid = document.querySelector("#video-grid");
  videoGrid.replaceChildren(
    ...visibleDirectories.map(makeVideoFolderCard),
    ...visibleVideos.map(makeVideoCard),
  );
  renderVideoBreadcrumb();
  const visibleCount = visibleDirectories.length + visibleVideos.length;
  document.querySelector("#videos-empty-state").hidden = visibleCount > 0;
  document.querySelector("#videos-empty-title").textContent = videos.length
    ? "No matching videos"
    : "No videos found";
  document.querySelector("#videos-empty-description").textContent = videos.length
    ? "Try another search or change the watched filter."
    : "There are no videos available from the MythTV backend.";
  document.querySelector("#videos-total").textContent = videos.length.toLocaleString();
  document.querySelector("#videos-result-note").textContent = activeVideoMode === "folders"
    ? `${visibleDirectories.length} folders · ${visibleVideos.length} videos in this directory · ${currentDirectory.matchingVideos} matching videos below`
    : `Showing ${visibleVideos.length} of ${videos.length} videos`;
}

async function loadVideos() {
  const videoGrid = document.querySelector("#video-grid");
  const status = document.querySelector("#videos-sync-note");
  videoGrid.setAttribute("aria-busy", "true");
  status.textContent = "Loading videos…";
  try {
    const response = await fetch(appPath("/api/videos"));
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || `Request failed (${response.status})`);
    videos = result.videos;
    status.textContent = "Connected to MythTV";
    renderVideos();
  } catch (error) {
    status.textContent = "MythTV backend unavailable";
    document.querySelector("#videos-empty-state").hidden = false;
    document.querySelector("#videos-empty-title").textContent = "Can’t reach MythTV";
    document.querySelector("#videos-empty-description").textContent = error.message;
  } finally {
    videoGrid.setAttribute("aria-busy", "false");
  }
}

async function updateVideoWatched(video) {
  const watched = !video.Watched;
  try {
    const response = await fetch(appPath(`/api/videos/${video.Id}/watched`), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ watched }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || `Request failed (${response.status})`);
    video.Watched = result.watched;
    renderVideos();
  } catch (error) {
    document.querySelector("#videos-sync-note").textContent = error.message;
  }
}

function makePoster(sourceUrl, badgeText = "", fallbackUrl = "", artworkType = "") {
  sourceUrl = sourceUrl || "";
  const poster = document.createElement("div");
  poster.className = "poster";
  if (artworkType === "coverart") poster.classList.add("poster-coverart");
  const fallback = document.createElement("span");
  fallback.className = "poster-fallback";
  fallback.textContent = "M";
  poster.append(fallback);
  const fallbackSource = fallbackUrl ? appLocalPath(fallbackUrl) : "";

  if (sourceUrl || fallbackUrl) {
    const imageSource = sourceUrl.startsWith("/api/artwork?")
      ? appLocalPath(sourceUrl)
      : sourceUrl.startsWith("/api/video-preview/")
        ? appLocalPath(sourceUrl)
      : sourceUrl.startsWith("/Content/GetImageFile?")
        ? appLocalPath(`/api/artwork?url=${encodeURIComponent(sourceUrl)}`)
        : fallbackSource
          ? fallbackSource
          : "";
    if (imageSource) {
      const image = document.createElement("img");
      image.src = imageSource;
      image.alt = "";
      image.loading = "lazy";
      image.addEventListener("load", () => fallback.remove(), { once: true });
      image.addEventListener("error", () => {
        poster.classList.remove("poster-coverart");
        if (fallbackSource && image.src !== new URL(fallbackSource, location.href).href) {
          image.src = fallbackSource;
        } else {
          image.remove();
        }
      });
      poster.prepend(image);
    }
  }

  if (badgeText) {
    const badge = document.createElement("span");
    badge.className = "poster-badge";
    badge.textContent = badgeText;
    poster.append(badge);
  }
  return poster;
}

function makeRecordingAction(label, iconParts, handler) {
  const button = document.createElement("button");
  button.className = "recording-action";
  button.type = "button";
  button.setAttribute("aria-label", label);
  button.title = label;
  const icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  icon.setAttribute("class", "recording-action-icon");
  icon.setAttribute("viewBox", "0 0 24 24");
  icon.setAttribute("aria-hidden", "true");
  for (const part of iconParts) {
    if (typeof part === "string") {
      const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      path.setAttribute("d", part);
      icon.append(path);
    } else {
      const text = document.createElementNS("http://www.w3.org/2000/svg", "text");
      text.setAttribute("x", part.x);
      text.setAttribute("y", part.y);
      text.textContent = part.text;
      icon.append(text);
    }
  }
  button.append(icon);
  button.addEventListener("click", handler);
  return button;
}

function showDeleteDialog(program) {
  pendingDelete = program;
  deleteCheckbox.checked = false;
  deleteButton.disabled = true;
  deleteStatus.textContent = "";
  document.querySelector("#delete-recording-name").textContent =
    program.SubTitle || program.Title || "Untitled recording";
  deleteDialog.showModal();
}

function matchesProgramFilter(program) {
  if (activeFilter === "watched") return isWatched(program);
  if (activeFilter === "unwatched") return !isWatched(program);
  return true;
}

function matchesGroupFilter(group) {
  if (activeFilter === "watched") return group.watched_count > 0;
  if (activeFilter === "unwatched") {
    return group.watched_count < group.recording_count;
  }
  return true;
}

function makeGroupCard(group, index) {
  const card = document.createElement("article");
  card.className = "program-card group-card";
  card.style.animationDelay = `${Math.min(index % 8, 8) * 22}ms`;
  const openButton = document.createElement("button");
  openButton.className = "group-open";
  openButton.type = "button";
  openButton.setAttribute(
    "aria-label",
    `Open ${group.title}, ${group.recording_count} ${group.recording_count === 1 ? "recording" : "recordings"}`,
  );
  const badge = group.watched_count === group.recording_count ? "Watched" : "";
  const poster = makePoster(
    group.artwork_url,
    badge,
    group.preview_url,
    group.artwork_type,
  );

  const info = document.createElement("div");
  info.className = "program-info program-info-overlay";
  const channel = document.createElement("p");
  channel.className = "program-channel";
  channel.textContent = group.channel_name || group.category || "Program";
  const title = document.createElement("h2");
  title.className = "program-title";
  title.textContent = group.title;
  const subtitle = document.createElement("p");
  subtitle.className = "group-count";
  subtitle.textContent = `${group.recording_count} ${group.recording_count === 1 ? "recording" : "recordings"}`;
  const meta = document.createElement("div");
  meta.className = "program-meta";
  const latest = document.createElement("time");
  latest.dateTime = group.latest_start || "";
  latest.textContent = `Latest ${formatDate(group.latest_start)}`;
  meta.append(latest);
  info.append(channel, title, subtitle, meta);
  poster.append(info);
  openButton.append(poster);
  openButton.addEventListener("click", () => loadGroup(group));
  card.append(openButton);
  return card;
}

function makeProgramCard(program, index) {
  const card = document.createElement("article");
  card.className = "program-card";
  card.style.animationDelay = `${Math.min(index % 8, 8) * 22}ms`;
  const artwork = artworkInfo(program, "coverart");
  const poster = makePoster(
    artwork?.URL || selectedGroupArtwork,
    isWatched(program) ? "Watched" : "",
    previewUrl(program),
    artwork?.Type || selectedGroupArtworkType,
  );
  const posterButton = document.createElement("button");
  posterButton.className = "recording-poster-button";
  posterButton.type = "button";
  posterButton.setAttribute(
    "aria-label",
    `Resume ${program.SubTitle || program.Title || "recording"}`,
  );
  posterButton.title = "Resume recording";
  posterButton.append(poster);
  const posterPlayIcon = document.createElement("span");
  posterPlayIcon.className = "poster-play-mark";
  posterPlayIcon.setAttribute("aria-hidden", "true");
  posterPlayIcon.innerHTML = '<svg viewBox="0 0 24 24"><path d="m8 5 12 7-12 7z" /></svg>';
  posterButton.append(posterPlayIcon);
  posterButton.addEventListener("click", () => openPlayer(program, "resume"));

  const info = document.createElement("div");
  info.className = "program-info program-info-overlay";
  const channel = document.createElement("p");
  channel.className = "program-channel";
  channel.textContent = program.Channel?.ChannelName || program.Category || "Recording";
  const title = document.createElement("h2");
  title.className = "program-title";
  title.textContent = program.SubTitle || program.Title || "Untitled recording";
  const subtitle = document.createElement("p");
  subtitle.className = "program-subtitle";
  const episode = episodeLabel(program);
  subtitle.textContent = episode || program.Category || " ";
  const description = document.createElement("p");
  description.className = "program-description";
  description.textContent = program.Description || "";
  const meta = document.createElement("div");
  meta.className = "program-meta";
  const date = document.createElement("time");
  date.dateTime = program.StartTime || "";
  date.textContent = formatDate(program.StartTime);
  const size = document.createElement("span");
  size.className = "program-size";
  size.textContent = formatSize(program.FileSize);
  meta.append(date, size);
  info.append(channel, title, subtitle, description, meta);
  const actions = document.createElement("div");
  actions.className = "recording-actions";
  actions.setAttribute("role", "group");
  actions.setAttribute("aria-label", "Recording actions");
  const recordingActions = [
    makeRecordingAction(
      "Play from beginning",
      [
        "M5 5v14l11-7zM19 7a8 8 0 1 1-2-2",
        "M19 3v4h-4",
        { x: "11", y: "16", text: "0" },
      ],
      () => openPlayer(program, "beginning"),
    ),
    makeRecordingAction(
      "Play from bookmark",
      ["M6 3h12v18l-6-4-6 4z"],
      () => openPlayer(program, "bookmark"),
    ),
  ];
  if (deleteDialog && CAN_DELETE) {
    recordingActions.push(makeRecordingAction(
      "Delete recording",
      ["M4 7h16M10 11v6m4-6v6M6 7l1 14h10l1-14M9 7V4h6v3"],
      () => showDeleteDialog(program),
    ));
  }
  actions.append(...recordingActions);
  poster.append(info);
  card.append(posterButton, actions);
  return card;
}

function renderGroups() {
  const query = searchInput.value.trim().toLocaleLowerCase();
  const visibleGroups = programGroups.filter((group) => {
    const searchable = [
      group.title,
      group.category,
      group.channel_name,
    ]
      .filter(Boolean)
      .join(" ")
      .toLocaleLowerCase();
    return matchesGroupFilter(group) && (!query || searchable.includes(query));
  });

  grid.replaceChildren(
    ...visibleGroups.map((group, index) => makeGroupCard(group, index)),
  );
  emptyState.hidden = visibleGroups.length > 0;
  document.querySelector("#empty-title").textContent = "No matching programs";
  document.querySelector("#empty-description").textContent = programGroups.length
    ? "Try another search or change the watched filter."
    : "There are no recordings available from the MythTV backend.";
  const recordingsShown = visibleGroups.reduce(
    (sum, group) => sum + group.recording_count,
    0,
  );
  resultNote.textContent = query || activeFilter !== "all"
    ? `Showing ${visibleGroups.length} groups · ${recordingsShown.toLocaleString()} recordings`
    : `Showing ${visibleGroups.length} program groups`;
  document.querySelector("#loaded-count").textContent =
    `${programGroups.length.toLocaleString()} groups`;
  document.querySelector("#total-count").textContent = totalRecordings.toLocaleString();
  document.querySelector("#total-label").textContent =
    `recordings across ${programGroups.length.toLocaleString()} program groups`;
  document.querySelector("#page-title").textContent = "Recordings";
  document.querySelector("#back-to-groups").hidden = true;
  searchInput.placeholder = "Search program groups";
}

function renderGroupPrograms() {
  const query = searchInput.value.trim().toLocaleLowerCase();
  const visiblePrograms = currentPrograms.filter((program) => {
    const searchable = [
      program.Title,
      program.SubTitle,
      program.Description,
      program.Category,
      program.Channel?.ChannelName,
    ]
      .filter(Boolean)
      .join(" ")
      .toLocaleLowerCase();
    return matchesProgramFilter(program) && (!query || searchable.includes(query));
  });

  grid.replaceChildren(
    ...visiblePrograms.map((program, index) => makeProgramCard(program, index)),
  );
  emptyState.hidden = visiblePrograms.length > 0;
  document.querySelector("#empty-title").textContent = "No matching recordings";
  document.querySelector("#empty-description").textContent =
    "Try another search or change the watched filter.";
  resultNote.textContent = `Showing ${visiblePrograms.length} of ${currentPrograms.length} recordings`;
  document.querySelector("#loaded-count").textContent =
    `${currentPrograms.length.toLocaleString()} recordings in this group`;
  document.querySelector("#total-count").textContent = currentPrograms.length.toLocaleString();
  document.querySelector("#total-label").textContent = "recordings in this group";
  document.querySelector("#page-title").textContent = selectedGroupTitle;
  backButton.hidden = false;
  searchInput.placeholder = "Search recordings in this group";
}

function renderCurrentView() {
  if (selectedGroupTitle) renderGroupPrograms();
  else renderGroups();
}

async function loadGroups() {
  if (loading) return;
  loading = true;
  grid.setAttribute("aria-busy", "true");
  syncNote.textContent = "Loading program groups…";

  try {
    const response = await fetch(appPath("/api/program-groups"));
    if (!response.ok) throw new Error(`Request failed (${response.status})`);
    const result = await response.json();
    programGroups = result.groups;
    totalRecordings = result.total;
    syncNote.textContent = "Connected to MythTV";
    renderGroups();
  } catch (error) {
    syncNote.textContent = "MythTV backend unavailable";
    emptyState.hidden = false;
    document.querySelector("#empty-title").textContent = "Can’t reach MythTV";
    document.querySelector("#empty-description").textContent =
      "Check that the local backend is running, then reload this page.";
    resultNote.textContent = error.message;
  } finally {
    loading = false;
    grid.setAttribute("aria-busy", "false");
  }
}

async function loadGroup(group) {
  if (loading) return;
  recordingGroupsScrollY = window.scrollY;
  loading = true;
  grid.setAttribute("aria-busy", "true");
  syncNote.textContent = "Loading recordings…";
  try {
    const response = await fetch(
      appPath(`/api/program-group?title=${encodeURIComponent(group.title)}`),
    );
    if (!response.ok) throw new Error(`Request failed (${response.status})`);
    const result = await response.json();
    selectedGroupTitle = result.title;
    selectedGroupArtwork = group.artwork_url;
    selectedGroupArtworkType = group.artwork_type;
    currentPrograms = result.programs;
    searchInput.value = "";
    syncNote.textContent = "Complete group";
    renderGroupPrograms();
  } catch (error) {
    syncNote.textContent = "Could not load this program group";
    resultNote.textContent = error.message;
  } finally {
    loading = false;
    grid.setAttribute("aria-busy", "false");
  }
}

function returnToGroups() {
  selectedGroupTitle = null;
  selectedGroupArtwork = null;
  selectedGroupArtworkType = null;
  currentPrograms = [];
  searchInput.value = "";
  renderGroups();
  window.scrollTo({ top: recordingGroupsScrollY, behavior: "instant" });
}

searchInput.addEventListener("input", renderCurrentView);
document.querySelector("#video-search").addEventListener("input", renderVideos);
document.querySelectorAll("[data-video-filter]").forEach((button) => {
  button.addEventListener("click", () => {
    activeVideoFilter = button.dataset.videoFilter;
    document.querySelectorAll("[data-video-filter]").forEach((item) => {
      const selected = item === button;
      item.classList.toggle("selected", selected);
      item.setAttribute("aria-pressed", String(selected));
    });
    renderVideos();
  });
});
document.querySelectorAll("[data-video-mode]").forEach((button) => {
  button.addEventListener("click", () => {
    activeVideoMode = button.dataset.videoMode;
    videoDirectoryPath = [];
    document.querySelectorAll("[data-video-mode]").forEach((item) => {
      const selected = item === button;
      item.classList.toggle("selected", selected);
      item.setAttribute("aria-pressed", String(selected));
    });
    renderVideos();
  });
});
document.querySelectorAll(".filter-button").forEach((button) => {
  button.addEventListener("click", () => {
    activeFilter = button.dataset.filter;
    document.querySelectorAll(".filter-button").forEach((item) => {
      const selected = item === button;
      item.classList.toggle("selected", selected);
      item.setAttribute("aria-pressed", String(selected));
    });
    renderCurrentView();
  });
});
backButton.addEventListener("click", returnToGroups);
if (deleteDialog) {
  deleteCheckbox.addEventListener("change", () => {
    deleteButton.disabled = !deleteCheckbox.checked || deleteInProgress;
  });
  deleteCancelButton.addEventListener("click", () => deleteDialog.close());
  deleteDialog.addEventListener("cancel", (event) => {
    if (deleteInProgress) event.preventDefault();
  });
  deleteDialog.addEventListener("close", () => {
    if (!deleteInProgress) pendingDelete = null;
  });
  deleteButton.addEventListener("click", async () => {
    if (!pendingDelete || !deleteCheckbox.checked || deleteInProgress) return;

    const program = pendingDelete;
    const recordedId = program.Recording?.RecordedId;
    if (!recordedId) return;
    deleteInProgress = true;
    deleteButton.disabled = true;
    deleteCancelButton.disabled = true;
    deleteCheckbox.disabled = true;
    deleteStatus.textContent = "Sending delete request to MythTV…";

    try {
      const response = await fetch(appPath(`/api/recordings/${recordedId}`), {
        method: "DELETE",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ confirmed: true }),
      });
      const result = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(result.error || `Request failed (${response.status})`);
      }

      currentPrograms = currentPrograms.filter(
        (item) => item.Recording?.RecordedId !== recordedId,
      );
      const group = programGroups.find((item) => item.title === (program.Title || "Untitled recording"));
      if (group) {
        group.recording_count = Math.max(0, group.recording_count - 1);
        if (isWatched(program)) group.watched_count = Math.max(0, group.watched_count - 1);
      }
      programGroups = programGroups.filter((item) => item.recording_count > 0);
      totalRecordings = Math.max(0, totalRecordings - 1);
      pendingDelete = null;
      deleteInProgress = false;
      deleteDialog.close();
      if (currentPrograms.length) renderGroupPrograms();
      else returnToGroups();
      syncNote.textContent = "Recording removed from the active library";
    } catch (error) {
      deleteInProgress = false;
      deleteCancelButton.disabled = false;
      deleteCheckbox.disabled = false;
      deleteButton.disabled = !deleteCheckbox.checked;
      deleteStatus.textContent = error.message;
    }
  });
}
document.querySelector("#player-close").addEventListener("click", closePlayer);
playerDialog.addEventListener("cancel", (event) => {
  event.preventDefault();
  closePlayer();
});
document.querySelector("#player-toggle").addEventListener("click", () => {
  if (playerVideo.paused) {
    playerVideo.play().catch(() => {
      playerStatus.textContent = "Unable to start playback.";
    });
  } else {
    playerVideo.pause();
  }
});
playerVideo.addEventListener("play", () => {
  const button = document.querySelector("#player-toggle");
  button.setAttribute("aria-label", "Pause");
  button.title = "Pause";
  button.innerHTML = '<svg class="player-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M8 5v14M16 5v14" /></svg>';
});
playerVideo.addEventListener("pause", () => {
  const button = document.querySelector("#player-toggle");
  button.setAttribute("aria-label", "Play");
  button.title = "Play";
  button.innerHTML = '<svg class="player-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="m8 5 12 7-12 7z" /></svg>';
});
playerVideo.addEventListener("playing", () => {
  playerStatus.textContent = usingWasm
    ? "Playing browser-side conversion"
    : usingTranscode
      ? "Playing live server conversion"
      : activeVideo
        ? "Playing MythTV video"
        : "Playing MythTV recording";
});
playerVideo.addEventListener("error", handleVideoError);
playerVideo.addEventListener("timeupdate", () => {
  updatePlaybackProgress();
  if (!smartSkip.checked || smartSkipBusy) return;
  const position = playbackPosition();
  const marker = commercialBreaks.find(
    (item) => position >= item.start && position < item.end,
  );
  if (marker) {
    smartSkipBusy = true;
    playerStatus.textContent = "Smart skip: commercial break skipped.";
    seekTo(marker.end + 0.25);
    window.setTimeout(() => { smartSkipBusy = false; }, 1500);
  }
});
playerVideo.addEventListener("ended", savePlaybackPosition);
document.querySelector("#jump-back").addEventListener("click", () => {
  seekTo(playbackPosition() - 10);
  setForwardSkipMode(true);
});
jumpForwardButton.addEventListener("click", () => {
  seekTo(playbackPosition() + (forwardSkipMode ? 5 : 30));
  if (forwardSkipMode) setForwardSkipMode(true);
});
document.querySelector("#jump-back-ten-minutes").addEventListener("click", () => {
  seekTo(playbackPosition() - 600);
});
document.querySelector("#jump-forward-ten-minutes").addEventListener("click", () => {
  seekTo(playbackPosition() + 600);
});
skipAdButton.addEventListener("click", () => {
  const position = playbackPosition();
  const marker = commercialBreaks.find(
    (item) => item.end > position && item.start < position + 120,
  );
  if (marker) seekTo(marker.end + 0.25);
});
playbackSeek.addEventListener("input", () => {
  const duration = playbackLength();
  document.querySelector("#playback-elapsed").textContent = formatPlaybackTime(
    (Number(playbackSeek.value) / 1000) * duration,
  );
});
playbackSeek.addEventListener("change", () => {
  const duration = playbackLength();
  seekTo((Number(playbackSeek.value) / 1000) * duration);
});
document.querySelector("#playback-speed").addEventListener("change", (event) => {
  playerVideo.playbackRate = Number(event.currentTarget.value);
  if ("preservesPitch" in playerVideo) playerVideo.preservesPitch = true;
});
function updatePlayerVolumeControls() {
  if (playerVideo.volume > 0) lastNonzeroVolume = playerVideo.volume;
  playerVolume.value = String(playerVideo.volume);
  const muted = playerVideo.muted || playerVideo.volume === 0;
  playerMute.setAttribute("aria-label", muted ? "Unmute" : "Mute");
  playerMute.title = muted ? "Unmute" : "Mute";
  playerMute.innerHTML = muted
    ? '<svg class="player-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M11 5 6 9H3v6h3l5 4V5Z" /><path d="m16 9 5 6m0-6-5 6" /></svg>'
    : '<svg class="player-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M11 5 6 9H3v6h3l5 4V5Z" /><path d="M15.5 8.5a5 5 0 0 1 0 7m3-10a9 9 0 0 1 0 13" /></svg>';
}
playerVolume.addEventListener("input", (event) => {
  playerVideo.volume = Number(event.currentTarget.value);
  if (playerVideo.volume > 0) playerVideo.muted = false;
});
playerMute.addEventListener("click", () => {
  if (playerVideo.muted || playerVideo.volume === 0) {
    if (playerVideo.volume === 0) playerVideo.volume = lastNonzeroVolume;
    playerVideo.muted = false;
  } else {
    lastNonzeroVolume = playerVideo.volume;
    playerVideo.muted = true;
  }
});
playerVideo.addEventListener("volumechange", updatePlayerVolumeControls);
updatePlayerVolumeControls();
setBookmarkButton.addEventListener("click", async () => {
  if (!activeRecording && !activeVideo) return;
  const generation = playerGeneration;
  const videoId = activeVideo?.Id;
  const recordedId = activeRecording?.Recording.RecordedId;
  const bookmarkPath = videoId
    ? `/api/videos/${videoId}/bookmark`
    : `/api/playback/${recordedId}/bookmark`;
  const position = playbackPosition();
  setBookmarkButton.disabled = true;
  try {
    const response = await fetch(appPath(bookmarkPath), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ position }),
    });
    const result = await response.json();
    if (generation !== playerGeneration) return;
    if (!response.ok) throw new Error(result.error || `Request failed (${response.status})`);
    if (activeVideo?.Id === videoId && videoPlaybackInfo) {
      videoPlaybackInfo.bookmark = position;
    } else if (activeRecording?.Recording.RecordedId === recordedId && playbackInfo) {
      playbackInfo.bookmark = Math.round(position * 1000);
    }
    playerStatus.textContent = `MythTV bookmark set at ${formatPlaybackTime(position)}`;
  } catch (error) {
    if (generation === playerGeneration) playerStatus.textContent = `Could not set MythTV bookmark: ${error.message}`;
  } finally {
    setBookmarkButton.disabled = false;
  }
});
document.querySelector("#subtitle-track").addEventListener("change", (event) => {
  selectSubtitle(event.currentTarget.value);
});
function isExpandedPlayer() {
  return (
    document.fullscreenElement === playerShell
    || playerShell.classList.contains("theater-mode")
  );
}

function updatePictureInPictureButton() {
  const supported = (
    document.pictureInPictureEnabled
    && typeof playerVideo.requestPictureInPicture === "function"
    && typeof document.exitPictureInPicture === "function"
  );
  const active = document.pictureInPictureElement === playerVideo;
  const fullscreen = document.fullscreenElement === playerShell;
  const label = !supported
    ? "Picture-in-Picture is not supported"
    : fullscreen
      ? "Exit fullscreen to detach player"
      : active
        ? "Return player from Picture-in-Picture"
        : "Detach player with Picture-in-Picture";
  playerDetachButton.disabled = !supported || fullscreen;
  playerDetachButton.setAttribute("aria-label", label);
  playerDetachButton.setAttribute("aria-pressed", String(active));
  playerDetachButton.title = label;
}

function updateFullscreenControls() {
  const fullscreen = document.fullscreenElement === playerShell;
  if (fullscreen) {
    playerShell.classList.remove("theater-mode");
    playerDialog.classList.remove("theater-mode");
  }
  const theater = playerShell.classList.contains("theater-mode");
  document.querySelector("#player-fullscreen").hidden = fullscreen;
  document.querySelector("#player-exit-fullscreen").hidden = !fullscreen;
  playerTheaterButton.hidden = fullscreen || theater;
  playerExitTheaterButton.hidden = !theater || fullscreen;
  playerShell.classList.toggle("controls-visible", fullscreen || theater);
  window.clearTimeout(controlsHideTimer);
  updatePictureInPictureButton();
  if (fullscreen || theater) revealExpandedPlayerControls();
}

function setTheaterMode(enabled) {
  const theater = Boolean(enabled && document.fullscreenElement !== playerShell);
  playerShell.classList.toggle("theater-mode", theater);
  playerDialog.classList.toggle("theater-mode", theater);
  updateFullscreenControls();
}

function revealExpandedPlayerControls() {
  if (!isExpandedPlayer()) return;
  playerShell.classList.add("controls-visible");
  window.clearTimeout(controlsHideTimer);
  controlsHideTimer = window.setTimeout(() => {
    if (playerOverlay.contains(document.activeElement)) return;
    if (isExpandedPlayer()) playerShell.classList.remove("controls-visible");
  }, 2500);
}

async function togglePictureInPicture() {
  if (
    !document.pictureInPictureEnabled
    || typeof playerVideo.requestPictureInPicture !== "function"
    || typeof document.exitPictureInPicture !== "function"
  ) {
    playerStatus.textContent = "Picture-in-Picture is not supported by this browser.";
    return;
  }
  if (document.fullscreenElement === playerShell) {
    playerStatus.textContent = "Exit fullscreen before detaching the player.";
    return;
  }
  try {
    if (document.pictureInPictureElement === playerVideo) {
      await document.exitPictureInPicture();
    } else {
      setTheaterMode(false);
      await playerVideo.requestPictureInPicture();
    }
  } catch (error) {
    playerStatus.textContent = `Could not detach player: ${error.message}`;
  }
  updatePictureInPictureButton();
}

playerTheaterButton.addEventListener("click", () => setTheaterMode(true));
playerExitTheaterButton.addEventListener("click", () => setTheaterMode(false));
playerDetachButton.addEventListener("click", togglePictureInPicture);
playerVideo.addEventListener("enterpictureinpicture", updatePictureInPictureButton);
playerVideo.addEventListener("leavepictureinpicture", updatePictureInPictureButton);

document.querySelector("#player-fullscreen").addEventListener("click", () => {
  if (!playerShell.requestFullscreen) {
    playerStatus.textContent = "Fullscreen is not supported by this browser.";
    return;
  }
  setTheaterMode(false);
  playerShell.requestFullscreen()
    .then(updateFullscreenControls)
    .catch((error) => { playerStatus.textContent = `Could not enter fullscreen: ${error.message}`; });
});
document.querySelector("#player-exit-fullscreen").addEventListener("click", () => {
  if (document.fullscreenElement === playerShell && document.exitFullscreen) {
    document.exitFullscreen().then(updateFullscreenControls);
  }
});
document.addEventListener("fullscreenchange", updateFullscreenControls);
playerShell.addEventListener("pointermove", revealExpandedPlayerControls);
playerShell.addEventListener("pointerdown", revealExpandedPlayerControls);
playerShell.addEventListener("touchstart", revealExpandedPlayerControls, { passive: true });
playerShell.addEventListener("keydown", revealExpandedPlayerControls);
playerShell.addEventListener("focusin", revealExpandedPlayerControls);
playerOverlay.addEventListener("focusout", (event) => {
  if (!playerOverlay.contains(event.relatedTarget)) revealExpandedPlayerControls();
});
updateFullscreenControls();

function focusPlayerControlInDirection(current, direction) {
  const selector = "button:not(:disabled):not([hidden]), input:not(:disabled), select:not(:disabled), summary, video[tabindex]";
  const currentRect = current.getBoundingClientRect();
  const currentX = currentRect.left + currentRect.width / 2;
  const currentY = currentRect.top + currentRect.height / 2;
  const candidates = [...playerDialog.querySelectorAll(selector)]
    .filter((element) => element !== current && element.getClientRects().length)
    .map((element) => {
      const rect = element.getBoundingClientRect();
      const deltaX = rect.left + rect.width / 2 - currentX;
      const deltaY = rect.top + rect.height / 2 - currentY;
      const primary = direction === "ArrowLeft" ? -deltaX
        : direction === "ArrowRight" ? deltaX
          : direction === "ArrowUp" ? -deltaY : deltaY;
      const secondary = direction === "ArrowLeft" || direction === "ArrowRight"
        ? Math.abs(deltaY)
        : Math.abs(deltaX);
      return { element, primary, secondary };
    })
    .filter(({ primary }) => primary > 0)
    .sort((left, right) =>
      left.primary + left.secondary * 2 - (right.primary + right.secondary * 2),
    );
  candidates[0]?.element.focus({ preventScroll: true });
}

document.addEventListener("keydown", (event) => {
  if (playerDialog.open) {
    if (["MediaPlayPause", "MediaPlay"].includes(event.key)) {
      event.preventDefault();
      if (playerVideo.paused) playerVideo.play().catch(() => {});
      else playerVideo.pause();
    } else if (event.key === "MediaPause") {
      event.preventDefault();
      playerVideo.pause();
    } else if (document.activeElement === playerVideo && event.key === "ArrowLeft") {
      event.preventDefault();
      seekTo(playbackPosition() - 10);
    } else if (document.activeElement === playerVideo && event.key === "ArrowRight") {
      event.preventDefault();
      seekTo(playbackPosition() + 30);
    } else if (document.activeElement === playerVideo && ["ArrowUp", "ArrowDown"].includes(event.key)) {
      event.preventDefault();
      (event.key === "ArrowDown"
        ? playbackSeek
        : document.querySelector("#player-close"))
        .focus({ preventScroll: true });
    } else if (document.activeElement === playerVideo && [" ", "Enter"].includes(event.key)) {
      event.preventDefault();
      document.querySelector("#player-toggle").click();
    } else if (
      ["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(event.key)
      && event.target instanceof HTMLButtonElement
    ) {
      focusPlayerControlInDirection(event.target, event.key);
      if (document.activeElement !== event.target) event.preventDefault();
    }
  } else if (event.key === "/" && document.activeElement !== searchInput) {
    event.preventDefault();
    (document.querySelector('[data-view-panel="videos"]').hidden
      ? searchInput
      : document.querySelector("#video-search")).focus();
  } else if (event.key === "Escape" && selectedGroupTitle) {
    returnToGroups();
  }
});

const viewButtons = [...document.querySelectorAll("[data-view]")];
const viewPanels = [...document.querySelectorAll("[data-view-panel]")];
const guideList = document.querySelector("#guide-list");
const guideStatus = document.querySelector("#guide-status");
const guideWindow = document.querySelector("#guide-window");
const guideSearchInput = document.querySelector("#guide-search");
const guideSearchCount = document.querySelector("#guide-search-count");
const recordingDialog = document.querySelector("#recording-dialog");
const recordingRuleForm = document.querySelector("#recording-rule-form");
const recordingRuleFields = document.querySelector("#recording-rule-fields");
const recordingDialogStatus = document.querySelector("#recording-dialog-status");
const recordingRuleDeleteButton = document.querySelector("#recording-rule-delete");
const guideStartTime = new Date();
guideStartTime.setMinutes(0, 0, 0);
let editingSchedule = null;
let guideData = null;
let guideDetailsCard = null;
let guideDetailsPinned = false;
let guideDetailsHideTimer = 0;
const guideDetailsPopover = document.createElement("aside");
guideDetailsPopover.className = "guide-details-popover";
guideDetailsPopover.id = "guide-details-popover";
guideDetailsPopover.setAttribute("aria-label", "Program details");
guideDetailsPopover.hidden = true;
document.body.append(guideDetailsPopover);

function makeElement(tagName, className, text) {
  const element = document.createElement(tagName);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

function formatProgramTime(start, end) {
  const formatter = new Intl.DateTimeFormat(undefined, {
    hour: "numeric",
    minute: "2-digit",
  });
  return `${formatter.format(new Date(start))}–${formatter.format(new Date(end))}`;
}

function formatGuideWindow(start, end) {
  const formatter = new Intl.DateTimeFormat(undefined, {
    weekday: "short",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
  return `${formatter.format(start)} – ${formatter.format(end)}`;
}

function makeGuideRecordButton(program, channel, statusElement, scheduled, withText = false) {
  const button = makeElement(
    "button",
    withText ? "guide-details-record-action" : "guide-event-action",
  );
  button.type = "button";
  button.setAttribute(
    "aria-label",
    `${scheduled ? "Edit recording rule for" : "Record"} ${program.Title || "program"}`,
  );
  button.title = scheduled ? "Edit recording rule" : "Record program";
  const icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  icon.setAttribute("viewBox", "0 0 24 24");
  icon.setAttribute("aria-hidden", "true");
  if (scheduled) {
    const pen = document.createElementNS("http://www.w3.org/2000/svg", "path");
    pen.setAttribute("d", "M12 20h9M16.5 3.5a2.1 2.1 0 0 1 3 3L8 18l-4 1 1-4Z");
    icon.append(pen);
  } else {
    const outer = document.createElementNS("http://www.w3.org/2000/svg", "circle");
    outer.setAttribute("cx", "12");
    outer.setAttribute("cy", "12");
    outer.setAttribute("r", "8");
    icon.append(outer);
    const dot = document.createElementNS("http://www.w3.org/2000/svg", "circle");
    dot.setAttribute("cx", "12");
    dot.setAttribute("cy", "12");
    dot.setAttribute("r", "3");
    dot.setAttribute("class", "guide-record-icon-dot");
    icon.append(dot);
  }
  button.append(icon);
  if (withText) button.append(document.createTextNode(scheduled ? "Edit rule" : "Record"));
  button.addEventListener("click", () => {
    closeGuideDetails();
    openRecordingEditor({
      program,
      channelId: Number(channel.ChanId || 0),
      recordId: Number(program.Recording?.RecordId || 0),
      statusElement,
    });
  });
  return button;
}

function positionGuideDetails() {
  if (guideDetailsPopover.hidden || !guideDetailsCard) return;
  const anchor = guideDetailsCard.getBoundingClientRect();
  const panel = guideDetailsPopover.getBoundingClientRect();
  const left = Math.max(12, Math.min(anchor.left, window.innerWidth - panel.width - 12));
  const below = anchor.bottom + 8;
  const top = below + panel.height <= window.innerHeight - 12
    ? below
    : Math.max(12, anchor.top - panel.height - 8);
  guideDetailsPopover.style.left = `${left}px`;
  guideDetailsPopover.style.top = `${top}px`;
}

function scheduleGuideDetailsHide() {
  window.clearTimeout(guideDetailsHideTimer);
  guideDetailsHideTimer = window.setTimeout(() => {
    if (!guideDetailsPinned) closeGuideDetails();
  }, 160);
}

function closeGuideDetails(restoreFocus = false) {
  window.clearTimeout(guideDetailsHideTimer);
  if (guideDetailsCard) {
    const trigger = guideDetailsCard.querySelector(".guide-event-info-trigger");
    guideDetailsCard.classList.remove("has-details");
    trigger?.setAttribute("aria-expanded", "false");
    trigger?.removeAttribute("aria-describedby");
  }
  guideDetailsPopover.hidden = true;
  guideDetailsPinned = false;
  if (restoreFocus) guideDetailsCard?.focus({ preventScroll: true });
  guideDetailsCard = null;
}

function showGuideDetails(program, channel, card, statusElement, pinned = false, focusAction = false) {
  if (guideDetailsPinned && guideDetailsCard !== card) return;
  window.clearTimeout(guideDetailsHideTimer);
  if (guideDetailsCard && guideDetailsCard !== card) {
    const previousTrigger = guideDetailsCard.querySelector(".guide-event-info-trigger");
    guideDetailsCard.classList.remove("has-details");
    previousTrigger?.setAttribute("aria-expanded", "false");
    previousTrigger?.removeAttribute("aria-describedby");
  }
  guideDetailsCard = card;
  guideDetailsPinned = pinned;
  card.classList.add("has-details");
  const trigger = card.querySelector(".guide-event-info-trigger");
  trigger?.setAttribute("aria-expanded", "true");
  trigger?.setAttribute("aria-describedby", guideDetailsPopover.id);

  const recording = program.Recording || {};
  const recordId = Number(recording.RecordId || 0);
  const recordingStatus = recording.StatusName || recording.RecTypeStatus || "";
  const scheduled = recordId > 0 || /^(will record|recording)$/i.test(recordingStatus);
  const channelName = [channel.ChanNum, channel.CallSign || channel.ChannelName].filter(Boolean).join(" · ");
  const header = makeElement("header", "guide-details-header");
  const heading = makeElement("div", "guide-details-heading");
  heading.append(makeElement("h2", "guide-details-title", program.Title || "Untitled program"));
  heading.querySelector(".guide-details-title").id = "guide-details-title";
  if (program.SubTitle) heading.append(makeElement("p", "guide-details-subtitle", program.SubTitle));
  const closeButton = makeElement("button", "guide-details-close", "×");
  closeButton.type = "button";
  closeButton.setAttribute("aria-label", "Close program details");
  closeButton.addEventListener("click", () => closeGuideDetails(true));
  header.append(heading, closeButton);

  const metadata = makeElement("dl", "guide-details-metadata");
  const metadataItems = [
    ["Time", formatProgramTime(program.StartTime, program.EndTime)],
    ["Channel", channelName || "Channel"],
    ["Category", program.Category || "Uncategorized"],
    ["Recording", scheduled ? recordingStatus || "Scheduled" : "Not scheduled"],
  ];
  for (const [label, value] of metadataItems) {
    const item = makeElement("div", "guide-details-meta-item");
    item.append(makeElement("dt", "guide-details-meta-label", label));
    item.append(makeElement("dd", "guide-details-meta-value", value));
    metadata.append(item);
  }
  const details = makeElement("div", "guide-details-description");
  if (program.Description) details.textContent = program.Description;
  else details.textContent = "No program description is available.";
  const actions = makeElement("div", "guide-details-actions");
  actions.append(makeGuideRecordButton(program, channel, statusElement, scheduled, true));
  guideDetailsPopover.replaceChildren(header, metadata, details, actions);
  guideDetailsPopover.setAttribute("role", pinned ? "dialog" : "region");
  guideDetailsPopover.setAttribute("aria-labelledby", "guide-details-title");
  guideDetailsPopover.hidden = false;
  positionGuideDetails();
  if (focusAction) guideDetailsPopover.querySelector("button:not(.guide-details-close)")?.focus({ preventScroll: true });
}

guideDetailsPopover.addEventListener("pointerenter", () => window.clearTimeout(guideDetailsHideTimer));
guideDetailsPopover.addEventListener("pointerleave", scheduleGuideDetailsHide);
guideDetailsPopover.addEventListener("focusout", (event) => {
  if (!guideDetailsPopover.contains(event.relatedTarget)) scheduleGuideDetailsHide();
});
document.addEventListener("pointerdown", (event) => {
  if (guideDetailsPinned && !guideDetailsPopover.contains(event.target) && !guideDetailsCard?.contains(event.target)) {
    closeGuideDetails();
  }
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !guideDetailsPopover.hidden) {
    event.stopPropagation();
    closeGuideDetails(true);
  }
});
window.addEventListener("resize", positionGuideDetails);
guideList.addEventListener("scroll", positionGuideDetails, { passive: true });

async function requestJson(path, options = {}) {
  const response = await fetch(appPath(path), options);
  const result = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(result.error || `Request failed (${response.status})`);
  }
  return result;
}

function makeScheduleButton(program, channelId, statusElement) {
  const scheduled = Number(program.Recording?.RecordId || 0) > 0;
  const button = makeElement(
    "button",
    "schedule-button",
    scheduled ? "Edit rule" : "Record",
  );
  button.type = "button";
  button.addEventListener("click", () => openRecordingEditor({
    program,
    channelId,
    recordId: Number(program.Recording?.RecordId || 0),
    statusElement,
  }));
  return button;
}

function recordingFieldControl(field, value, choices) {
  let control;
  if (field.type === "select") {
    control = document.createElement("select");
    const options = field.options(choices);
    if (value !== undefined && value !== null && !options.some((option) => String(option.value) === String(value))) {
      options.unshift({ value, label: String(value) });
    }
    for (const item of options) {
      const option = document.createElement("option");
      option.value = String(item.value);
      option.textContent = item.label;
      control.append(option);
    }
  } else if (field.type === "textarea") {
    control = document.createElement("textarea");
    control.rows = 3;
    control.value = value ?? "";
  } else {
    control = document.createElement("input");
    control.type = field.type || "text";
    if (field.type === "datetime-local" && value) {
      const date = new Date(value);
      control.value = Number.isNaN(date.getTime()) ? "" : new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
    } else if (field.type === "time" && value) {
      control.value = String(value).slice(0, 5);
    } else if (field.type === "checkbox") {
      control.checked = Boolean(value);
    } else {
      control.value = value ?? "";
    }
  }
  control.dataset.ruleField = field.key;
  if (field.type === "number") control.step = "1";
  if (field.required) control.required = true;
  return control;
}

function renderRecordingRuleFields(rule, choices) {
  const recGroupOptions = choices.recording_groups?.RecGroupList || [];
  const storageGroupOptions = choices.storage_groups?.RecStorageGroupList || [];
  const playGroupOptions = choices.play_groups?.PlayGroupList || [];
  const inputOptions = choices.inputs?.InputList?.Inputs || [];
  const filters = choices.filters?.RecRuleFilterList?.RecRuleFilters || [];
  const staticOptions = (values) => () => values.map((value) => ({ value, label: value }));
  const fields = [
    {
      title: "Program",
      fields: [
        { key: "Title", label: "Title" },
        { key: "SubTitle", label: "Subtitle" },
        { key: "Description", label: "Description", type: "textarea" },
        { key: "Category", label: "Category" },
        { key: "StartTime", label: "Start time", type: "datetime-local", required: true },
        { key: "EndTime", label: "End time", type: "datetime-local", required: true },
        { key: "ChanId", label: "Channel ID", type: "number" },
        { key: "CallSign", label: "Call sign" },
        { key: "Season", label: "Season", type: "number" },
        { key: "Episode", label: "Episode", type: "number" },
        { key: "SeriesId", label: "Series ID" },
        { key: "ProgramId", label: "Program ID" },
        { key: "Inetref", label: "Internet reference" },
      ],
    },
    {
      title: "Recording rule",
      fields: [
        { key: "Type", label: "Record", type: "select", options: staticOptions(["Record One", "Record All", "Record Daily", "Record Weekly", "Single Record", "Override Recording", "Do not Record", "Recording Template"]) },
        { key: "SearchType", label: "Search", type: "select", options: staticOptions(["None", "Title Search", "Keyword Search", "People Search", "Power Search", "Manual Search"]) },
        { key: "Inactive", label: "Rule inactive", type: "checkbox" },
        { key: "FindDay", label: "Find day", type: "number" },
        { key: "FindTime", label: "Find time", type: "time" },
        { key: "RecPriority", label: "Recording priority", type: "number" },
        {
          key: "PreferredInput",
          label: "Preferred input",
          type: "select",
          options: () => [
            { value: 0, label: "Any available input" },
            ...inputOptions.map((input) => ({ value: input.Id, label: input.DisplayName || input.InputName || `Input ${input.Id}` })),
          ],
        },
        { key: "StartOffset", label: "Start padding (minutes)", type: "number" },
        { key: "EndOffset", label: "End padding (minutes)", type: "number" },
        { key: "DupMethod", label: "Duplicate matching", type: "select", options: staticOptions(["None", "Subtitle", "Description", "Subtitle and Description", "Subtitle then Description"]) },
        { key: "DupIn", label: "Check duplicates in", type: "select", options: staticOptions(["Current Recordings", "Previous Recordings", "All Recordings"]) },
        { key: "NewEpisOnly", label: "New episodes only", type: "checkbox" },
      ],
    },
    {
      title: "Storage and limits",
      fields: [
        { key: "RecProfile", label: "Recording profile" },
        { key: "RecGroup", label: "Recording group", type: "select", options: () => recGroupOptions.map((value) => ({ value, label: value })) },
        { key: "StorageGroup", label: "Storage group", type: "select", options: () => storageGroupOptions.map((value) => ({ value, label: value })) },
        { key: "PlayGroup", label: "Playback group", type: "select", options: () => playGroupOptions.map((value) => ({ value, label: value })) },
        { key: "AutoExpire", label: "Allow automatic expiration", type: "checkbox" },
        { key: "MaxEpisodes", label: "Maximum episodes", type: "number" },
        { key: "MaxNewest", label: "Keep newest episodes", type: "checkbox" },
        { key: "AutoExtend", label: "Automatic sports extension", type: "select", options: staticOptions(["None", "ESPN", "MLB"]) },
        { key: "Transcoder", label: "Transcoder", type: "number" },
      ],
    },
    {
      title: "Filters and processing",
      fields: [
        { key: "Filter", label: "Rule filters", type: "filters" },
        { key: "AutoCommflag", label: "Flag commercials", type: "checkbox" },
        { key: "AutoTranscode", label: "Transcode after recording", type: "checkbox" },
        { key: "AutoMetaLookup", label: "Look up metadata", type: "checkbox" },
        ...[1, 2, 3, 4].map((job) => ({ key: `AutoUserJob${job}`, label: `Run user job ${job}`, type: "checkbox" })),
      ],
    },
  ];

  const sections = fields.map((section) => {
    const element = makeElement("section", "recording-rule-section");
    element.append(makeElement("h3", "recording-rule-section-title", section.title));
    const grid = makeElement("div", "recording-rule-grid");
    for (const field of section.fields) {
      if (field.type === "filters") {
        const fieldset = makeElement("fieldset", "recording-filter-fieldset");
        fieldset.append(makeElement("legend", "recording-field-label", field.label));
        const knownMask = filters.reduce((mask, filter) => mask | (1 << filter.Id), 0);
        fieldset.dataset.unrepresentedMask = String((Number(rule.Filter) || 0) & ~knownMask);
        for (const filter of filters) {
          const label = makeElement("label", "recording-filter-option");
          const checkbox = document.createElement("input");
          checkbox.type = "checkbox";
          checkbox.dataset.filterBit = String(filter.Id);
          checkbox.checked = ((Number(rule.Filter) || 0) & (1 << filter.Id)) !== 0;
          label.append(checkbox, document.createTextNode(filter.Description));
          fieldset.append(label);
        }
        grid.append(fieldset);
        continue;
      }
      const label = makeElement("label", field.type === "checkbox" ? "recording-toggle" : "recording-field");
      const control = recordingFieldControl(field, rule[field.key], choices);
      if (field.type === "checkbox") {
        label.append(control, makeElement("span", "recording-field-label", field.label));
      } else {
        label.append(makeElement("span", "recording-field-label", field.label), control);
      }
      grid.append(label);
    }
    element.append(grid);
    return element;
  });
  recordingRuleFields.replaceChildren(...sections);
}

async function openRecordingEditor({ program, channelId, recordId = 0, statusElement }) {
  recordingDialogStatus.textContent = "Loading recording options…";
  document.querySelector("#recording-dialog-title").textContent = recordId ? "Edit recording rule" : "Set up recording";
  document.querySelector("#recording-dialog-subtitle").textContent = [program.Title, program.SubTitle].filter(Boolean).join(" · ");
  document.querySelector("#recording-dialog-save").disabled = true;
  recordingRuleDeleteButton.hidden = !CAN_DELETE || !recordId;
  recordingRuleDeleteButton.disabled = true;
  recordingRuleFields.replaceChildren();
  recordingDialog.showModal();
  try {
    const query = new URLSearchParams(recordId
      ? { record_id: String(recordId) }
      : { channel_id: String(channelId), start_time: program.StartTime });
    const result = await requestJson(`/api/schedules/options?${query}`);
    const rule = { ...result.rule };
    const resolvedRecordId = recordId || Number(rule.Id || 0);
    if (!resolvedRecordId) rule.Type = "Record One";
    document.querySelector("#recording-dialog-title").textContent = resolvedRecordId ? "Edit recording rule" : "Set up recording";
    recordingRuleDeleteButton.hidden = !CAN_DELETE || !resolvedRecordId;
    editingSchedule = { recordId: resolvedRecordId, channelId, startTime: program.StartTime, statusElement, program };
    renderRecordingRuleFields(rule, result.choices);
    recordingDialogStatus.textContent = "Choose how MythTV should handle this program.";
    document.querySelector("#recording-dialog-save").disabled = false;
    recordingRuleDeleteButton.disabled = false;
  } catch (error) {
    recordingDialogStatus.textContent = error.message;
  }
}

function collectRecordingRule() {
  const rule = {};
  for (const control of recordingRuleForm.querySelectorAll("[data-rule-field]")) {
    const key = control.dataset.ruleField;
    if (control.type === "checkbox") rule[key] = control.checked;
    else if (control.type === "number") rule[key] = Number(control.value || 0);
    else if (control.type === "datetime-local") rule[key] = new Date(control.value).toISOString();
    else if (control.type === "time") rule[key] = control.value ? `${control.value}:00` : "00:00:00";
    else rule[key] = control.value;
  }
  const filterSet = recordingRuleForm.querySelector(".recording-filter-fieldset");
  if (filterSet) {
    rule.Filter = Number(filterSet.dataset.unrepresentedMask) || 0;
    for (const checkbox of filterSet.querySelectorAll("[data-filter-bit]")) {
      if (checkbox.checked) rule.Filter |= 1 << Number(checkbox.dataset.filterBit);
    }
  }
  return rule;
}

recordingRuleForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!editingSchedule || !recordingRuleForm.reportValidity()) return;
  const saveButton = document.querySelector("#recording-dialog-save");
  saveButton.disabled = true;
  recordingDialogStatus.textContent = "Saving recording rule…";
  try {
    const { recordId, channelId, startTime, statusElement } = editingSchedule;
    const path = recordId ? `/api/schedules/${recordId}` : "/api/schedules";
    const result = await requestJson(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ channel_id: channelId, start_time: startTime, rule: collectRecordingRule() }),
    });
    recordingDialog.close();
    statusElement.textContent = recordId ? "Recording rule updated in MythTV." : "Recording scheduled in MythTV.";
    const upcomingButton = document.querySelector('[data-view="upcoming"]');
    if (upcomingButton?.dataset.loaded === "true") loadUpcoming();
    if (document.querySelector('[data-view="guide"]')?.classList.contains("active")) loadGuide();
    if (result.existing) statusElement.textContent = "This program already has a recording rule.";
  } catch (error) {
    recordingDialogStatus.textContent = error.message;
    saveButton.disabled = false;
  }
});

recordingRuleDeleteButton.addEventListener("click", async () => {
  if (!editingSchedule?.recordId) return;
  const title = editingSchedule.program.Title || "this program";
  if (!window.confirm(`Delete the recording rule for ${title}?`)) return;
  recordingRuleDeleteButton.disabled = true;
  document.querySelector("#recording-dialog-save").disabled = true;
  recordingDialogStatus.textContent = "Deleting recording rule…";
  try {
    const { recordId, statusElement } = editingSchedule;
    await requestJson(`/api/schedules/${recordId}/delete`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ confirm: true }),
    });
    recordingDialog.close();
    statusElement.textContent = `Recording rule for ${title} deleted from MythTV.`;
    const upcomingButton = document.querySelector('[data-view="upcoming"]');
    if (upcomingButton?.dataset.loaded === "true") loadUpcoming();
    if (document.querySelector('[data-view="guide"]')?.classList.contains("active")) loadGuide();
  } catch (error) {
    recordingDialogStatus.textContent = error.message;
    recordingRuleDeleteButton.disabled = false;
    document.querySelector("#recording-dialog-save").disabled = false;
  }
});

document.querySelector("#recording-dialog-cancel").addEventListener("click", () => recordingDialog.close());
document.querySelector("#recording-dialog-close").addEventListener("click", () => recordingDialog.close());

async function scheduleProgram(program, channelId, button, statusElement) {
  button.disabled = true;
  setScheduleButtonLabel(button, "Scheduling…");
  statusElement.textContent = "Sending schedule to MythTV…";
  try {
    const result = await requestJson("/api/schedules", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        channel_id: channelId,
        start_time: program.StartTime,
      }),
    });
    setScheduleButtonLabel(button, result.existing ? "Already scheduled" : "Scheduled");
    statusElement.textContent = result.existing
      ? "This program already has a recording rule."
      : "Recording scheduled in MythTV.";
    const upcomingButton = document.querySelector('[data-view="upcoming"]');
    if (upcomingButton?.dataset.loaded === "true") loadUpcoming();
  } catch (error) {
    button.disabled = false;
    setScheduleButtonLabel(button, "Record this program");
    statusElement.textContent = error.message;
  }
}

function setScheduleButtonLabel(button, label) {
  const text = button.querySelector("[data-schedule-label]");
  if (text) text.textContent = label;
  else button.textContent = label;
}

function renderChannelPrograms(container, channels, onlyCurrent, statusElement) {
  const now = Date.now();
  const fragment = document.createDocumentFragment();
  let rendered = 0;
  for (const channel of channels) {
    const programs = (channel.Programs || []).filter((program) => {
      if (!onlyCurrent) return true;
      const start = Date.parse(program.StartTime || "");
      const end = Date.parse(program.EndTime || "");
      return Number.isFinite(start) && Number.isFinite(end) && start <= now && now < end;
    });
    if (!programs.length) continue;

    const row = makeElement("article", "guide-channel");
    const channelTitle = channel.ChanNum
      ? `${channel.ChanNum} · ${channel.CallSign || channel.ChannelName || "Channel"}`
      : channel.CallSign || channel.ChannelName || "Channel";
    const channelHeader = makeElement("div", "guide-channel-heading");
    const fallback = makeElement(
      "span",
      "guide-channel-icon-fallback",
      (channel.CallSign || channel.ChanNum || "TV").slice(0, 2),
    );
    const iconFilename = channel.Icon || "";
    if (/^[A-Za-z0-9_. -]{1,255}$/.test(iconFilename)) {
      const icon = makeElement("img", "guide-channel-icon");
      icon.src = appPath(`/api/channel-icon?filename=${encodeURIComponent(iconFilename)}`);
      icon.alt = "";
      icon.loading = "lazy";
      icon.addEventListener("load", () => { fallback.hidden = true; }, { once: true });
      icon.addEventListener("error", () => icon.remove(), { once: true });
      channelHeader.append(icon);
    }
    channelHeader.append(fallback, makeElement("h2", "guide-channel-title", channelTitle));
    row.append(channelHeader);
    const programList = makeElement("div", "guide-programs");
    for (const program of programs) {
      const card = makeElement("article", "guide-program");
      const programArtwork = artworkUrl(program);
      if (programArtwork) {
        const image = makeElement("img", "guide-program-art");
        image.src = programArtwork;
        image.alt = "";
        image.loading = "lazy";
        image.addEventListener("error", () => image.remove(), { once: true });
        card.append(image);
      }
      card.append(makeElement("time", "guide-program-time", formatProgramTime(program.StartTime, program.EndTime)));
      card.append(makeElement("h3", "guide-program-title", program.Title || "Untitled program"));
      if (program.SubTitle) card.append(makeElement("p", "guide-program-subtitle", program.SubTitle));
      if (program.Category) card.append(makeElement("p", "guide-program-category", program.Category));
      const recording = program.Recording || {};
      if (recording.StatusName?.toLowerCase() === "recording" && Number(recording.RecordedId) > 0) {
        const watchButton = makeElement("button", "schedule-button", "Watch in player");
        watchButton.type = "button";
        watchButton.addEventListener("click", () => openPlayer(program));
        card.append(watchButton);
      }
      card.append(makeScheduleButton(program, channel.ChanId, statusElement));
      programList.append(card);
      rendered += 1;
    }
    row.append(programList);
    fragment.append(row);
  }
  container.replaceChildren(fragment);
  return rendered;
}

function renderGuideTimeline(container, channels, start, end, searchTerm = "") {
  closeGuideDetails();
  const windowStart = start.getTime();
  const windowEnd = end.getTime();
  const duration = windowEnd - windowStart;
  const normalizedSearch = searchTerm.trim().toLocaleLowerCase();
  const trackWidth = Math.max(720, duration / 3600000 * 260);
  const board = makeElement("div", "guide-board");
  board.dataset.windowStart = String(windowStart);
  board.dataset.windowEnd = String(windowEnd);
  board.style.setProperty("--guide-track-width", `${trackWidth}px`);

  const axis = makeElement("div", "guide-time-axis");
  const channelLabel = makeElement("div", "guide-axis-channel", "Channels");
  const axisTrack = makeElement("div", "guide-axis-track");
  axisTrack.style.width = `${trackWidth}px`;
  const tickMinutes = 30;
  const tickCount = Math.floor(duration / (tickMinutes * 60000));
  for (let index = 0; index < tickCount; index += 1) {
    const tickTime = windowStart + index * tickMinutes * 60000;
    const tick = makeElement("time", "guide-time-tick", new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit" }).format(tickTime));
    tick.dateTime = new Date(tickTime).toISOString();
    tick.style.left = `${index / tickCount * trackWidth}px`;
    axisTrack.append(tick);
  }
  const axisNow = makeElement("span", "guide-now-line guide-axis-now");
  axisNow.setAttribute("aria-hidden", "true");
  axisTrack.append(axisNow);
  axis.append(channelLabel, axisTrack);
  board.append(axis);

  let rendered = 0;
  for (const channel of channels) {
    const channelText = [channel.ChanNum, channel.CallSign, channel.ChannelName]
      .filter(Boolean)
      .join(" ")
      .toLocaleLowerCase();
    const channelMatches = normalizedSearch && channelText.includes(normalizedSearch);
    const programs = (channel.Programs || []).filter((program) => {
      const startTime = Date.parse(program.StartTime || "");
      const endTime = Date.parse(program.EndTime || "");
      if (!Number.isFinite(startTime) || !Number.isFinite(endTime) || endTime <= windowStart || startTime >= windowEnd) return false;
      if (!normalizedSearch || channelMatches) return true;
      return [program.Title, program.SubTitle, program.Description, program.Category]
        .filter(Boolean)
        .some((value) => value.toLocaleLowerCase().includes(normalizedSearch));
    });
    if (!programs.length) continue;

    const row = makeElement("div", "guide-timeline-row");
    const rail = makeElement("div", "guide-timeline-channel");
    const iconFilename = channel.Icon || "";
    if (/^[A-Za-z0-9_. -]{1,255}$/.test(iconFilename)) {
      const icon = makeElement("img", "guide-channel-icon");
      icon.src = appPath(`/api/channel-icon?filename=${encodeURIComponent(iconFilename)}`);
      icon.alt = "";
      icon.loading = "lazy";
      icon.addEventListener("error", () => icon.remove(), { once: true });
      rail.append(icon);
    } else {
      rail.append(makeElement("span", "guide-channel-icon-fallback", (channel.CallSign || channel.ChanNum || "TV").slice(0, 2)));
    }
    const channelTitle = channel.ChanNum
      ? `${channel.ChanNum} · ${channel.CallSign || channel.ChannelName || "Channel"}`
      : channel.CallSign || channel.ChannelName || "Channel";
    rail.append(makeElement("span", "guide-timeline-channel-name", channelTitle));

    const track = makeElement("div", "guide-timeline-track");
    track.style.width = `${trackWidth}px`;
    const nowLine = makeElement("span", "guide-now-line");
    nowLine.setAttribute("aria-hidden", "true");
    track.append(nowLine);
    for (const program of programs) {
      const programStart = Math.max(windowStart, Date.parse(program.StartTime));
      const programEnd = Math.min(windowEnd, Date.parse(program.EndTime));
      const left = (programStart - windowStart) / duration * trackWidth;
      const width = (programEnd - programStart) / duration * trackWidth;
      const card = makeElement("article", "guide-event-card");
      card.setAttribute("role", "group");
      card.style.left = `${left}px`;
      card.style.width = `${Math.max(1, Math.min(width, trackWidth - left))}px`;
      card.dataset.compact = String(width < 150);
      card.dataset.actionHidden = String(width < 54);
      const colorKey = [...String(program.Category || program.Title || "").toLowerCase()].reduce((sum, character) => sum + character.charCodeAt(0), 0) % 6;
      card.dataset.color = String(colorKey);
      const recording = program.Recording || {};
      const recordId = Number(recording.RecordId || 0);
      const recordingStatus = recording.StatusName || recording.RecTypeStatus || "";
      const scheduled = recordId > 0 || /^(will record|recording)$/i.test(recordingStatus);
      card.dataset.scheduled = String(scheduled);
      const artwork = artworkUrl(program);
      if (artwork) {
        const image = makeElement("img", "guide-event-art");
        image.src = artwork;
        image.alt = "";
        image.loading = "lazy";
        image.addEventListener("error", () => image.remove(), { once: true });
        card.append(image);
      }
      const content = makeElement("div", "guide-event-content guide-event-info-trigger");
      content.tabIndex = 0;
      content.setAttribute("role", "button");
      content.setAttribute("aria-label", `${program.Title || "Program"}, ${formatProgramTime(program.StartTime, program.EndTime)}. Show program details.`);
      content.setAttribute("aria-expanded", "false");
      content.setAttribute("aria-controls", guideDetailsPopover.id);
      content.append(makeElement("time", "guide-event-time", formatProgramTime(program.StartTime, program.EndTime)));
      content.append(makeElement("h3", "guide-event-title", program.Title || "Untitled program"));
      if (program.SubTitle) content.append(makeElement("p", "guide-event-subtitle", program.SubTitle));
      if (scheduled) content.append(makeElement("span", "guide-event-scheduled", "Scheduled"));
      const action = makeGuideRecordButton(program, channel, guideStatus, scheduled);
      card.append(content, action);
      card.addEventListener("pointerenter", () => {
        if (!guideDetailsPinned) showGuideDetails(program, channel, card, guideStatus);
      });
      card.addEventListener("pointerleave", (event) => {
        if (!guideDetailsPopover.contains(event.relatedTarget)) scheduleGuideDetailsHide();
      });
      card.addEventListener("focusin", () => {
        if (!guideDetailsPinned) showGuideDetails(program, channel, card, guideStatus);
      });
      card.addEventListener("focusout", (event) => {
        if (!card.contains(event.relatedTarget)) scheduleGuideDetailsHide();
      });
      const toggleDetails = (focusAction = false) => {
        const alreadyPinned = guideDetailsPinned && guideDetailsCard === card;
        if (alreadyPinned) closeGuideDetails();
        else showGuideDetails(program, channel, card, guideStatus, true, focusAction);
      };
      content.addEventListener("click", () => toggleDetails());
      content.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          toggleDetails(true);
        }
      });
      track.append(card);
      rendered += 1;
    }
    row.append(rail, track);
    board.append(row);
  }
  container.replaceChildren(board);
  updateGuideNowIndicator();
  return rendered;
}

function updateGuideNowIndicator() {
  const now = Date.now();
  for (const board of document.querySelectorAll(".guide-board")) {
    const start = Number(board.dataset.windowStart);
    const end = Number(board.dataset.windowEnd);
    const width = Number.parseFloat(board.style.getPropertyValue("--guide-track-width"));
    const ratio = (now - start) / (end - start);
    const visible = ratio >= 0 && ratio <= 1;
    const position = `${ratio * width}px`;
    for (const line of board.querySelectorAll(".guide-now-line")) {
      line.style.left = position;
      line.hidden = !visible;
    }
  }
}

async function loadGuide(onlyCurrent = false) {
  const start = onlyCurrent ? new Date(Date.now() - 60 * 60 * 1000) : new Date(guideStartTime);
  if (onlyCurrent) start.setMinutes(0, 0, 0);
  const end = new Date(start.getTime() + (onlyCurrent ? 3 : 4) * 60 * 60 * 1000);
  const list = onlyCurrent ? document.querySelector("#live-list") : guideList;
  const statusElement = onlyCurrent ? document.querySelector("#live-status") : guideStatus;
  statusElement.textContent = "Loading program data from MythTV…";
  try {
    const query = new URLSearchParams({ start: start.toISOString(), end: end.toISOString() });
    const result = await requestJson(`/api/guide?${query}`);
    let rendered;
    if (onlyCurrent) {
      rendered = renderChannelPrograms(list, result.channels, true, statusElement);
      document.querySelector("#live-summary").textContent = `${rendered} programs currently airing across visible channels.`;
      statusElement.textContent = rendered ? "Current channel listings" : "No current program data is available.";
    } else {
      guideData = { channels: result.channels, start, end };
      rendered = renderGuideTimeline(list, result.channels, start, end, guideSearchInput.value);
      guideWindow.textContent = formatGuideWindow(start, end);
      const visibleChannels = guideList.querySelectorAll(".guide-timeline-row").length;
      const query = guideSearchInput.value.trim();
      guideSearchCount.textContent = query ? `${rendered} found` : "";
      statusElement.textContent = query
        ? `${rendered} matching programs across ${visibleChannels} channels in this guide window.`
        : `${rendered} programs across ${result.channels.length} channels.`;
    }
  } catch (error) {
    list.replaceChildren(makeElement("p", "service-empty", error.message));
    statusElement.textContent = "Could not load program data.";
  }
}

guideSearchInput.addEventListener("input", () => {
  if (!guideData) return;
  const { channels, start, end } = guideData;
  const query = guideSearchInput.value.trim();
  const rendered = renderGuideTimeline(guideList, channels, start, end, query);
  const visibleChannels = guideList.querySelectorAll(".guide-timeline-row").length;
  guideSearchCount.textContent = query ? `${rendered} found` : "";
  guideStatus.textContent = query
    ? `${rendered} matching programs across ${visibleChannels} channels in this guide window.`
    : `${rendered} programs across ${channels.length} channels.`;
});

function appendMetric(container, label, value) {
  const metric = makeElement("div", "status-metric");
  metric.append(makeElement("span", "status-metric-label", label));
  metric.append(makeElement("strong", "status-metric-value", String(value)));
  container.append(metric);
}

async function loadStatus() {
  const metrics = document.querySelector("#status-metrics");
  const encoders = document.querySelector("#encoder-list");
  const summary = document.querySelector("#status-summary");
  summary.textContent = "Checking backend…";
  try {
    const result = await requestJson("/api/backend-status");
    const status = result.status;
    const encoderList = Array.isArray(status.Encoders) ? status.Encoders : [];
    const connected = encoderList.filter((encoder) => encoder.Connected).length;
    metrics.replaceChildren();
    appendMetric(metrics, "Version", status.Version || "Unknown");
    appendMetric(metrics, "Backends", Array.isArray(status.Backends) ? status.Backends.length : 0);
    appendMetric(metrics, "Encoders", encoderList.length);
    appendMetric(metrics, "Connected", `${connected} / ${encoderList.length}`);
    summary.textContent = status.AsOf ? `Updated ${formatDate(status.AsOf)}` : "Backend status received";
    const cards = encoderList.map((encoder) => {
      const card = makeElement("article", "service-row encoder-row");
      const heading = makeElement("div", "service-row-heading");
      heading.append(makeElement("h3", "service-row-title", `Encoder ${encoder.Id}`));
      heading.append(makeElement("span", encoder.Connected ? "service-state is-online" : "service-state", encoder.Connected ? "Connected" : "Offline"));
      card.append(heading);
      card.append(makeElement("p", "service-row-meta", encoder.HostName || "Host unknown"));
      card.append(makeElement("p", "service-row-meta", `State ${encoder.State ?? "unknown"}${encoder.Recording ? " · Recording" : " · Idle"}`));
      return card;
    });
    encoders.replaceChildren(...cards);
  } catch (error) {
    summary.textContent = error.message;
    metrics.replaceChildren();
    encoders.replaceChildren(makeElement("p", "service-empty", "Backend status is unavailable."));
  }
}

async function loadUpcoming() {
  const list = document.querySelector("#upcoming-list");
  const summary = document.querySelector("#upcoming-summary");
  const statusElement = document.querySelector("#upcoming-status");
  statusElement.textContent = "Loading scheduled programs…";
  try {
    const result = await requestJson("/api/upcoming?start=0&count=100");
    summary.textContent = `${result.total.toLocaleString()} scheduled programs`;
    const cards = result.programs.map((program) => {
      const card = makeElement("article", "service-row");
      card.append(makeElement("time", "service-row-date", formatProgramTime(program.StartTime, program.EndTime)));
      card.append(makeElement("h2", "service-row-title", program.Title || "Untitled program"));
      if (program.SubTitle) card.append(makeElement("p", "service-row-subtitle", program.SubTitle));
      const channel = program.Channel || {};
      const channelName = [channel.ChanNum, channel.CallSign || channel.ChannelName].filter(Boolean).join(" · ");
      card.append(makeElement("p", "service-row-meta", `${formatDate(program.StartTime)}${channelName ? ` · ${channelName}` : ""}`));
      card.append(makeElement("span", "service-state is-online", program.Recording?.RecTypeStatus || program.Recording?.StatusName || "Scheduled"));
      const recordId = Number(program.Recording?.RecordId || 0);
      if (recordId > 0) {
        const editButton = makeElement("button", "schedule-button", "Edit options");
        editButton.type = "button";
        editButton.addEventListener("click", () => openRecordingEditor({
          program,
          channelId: Number(channel.ChanId || 0),
          recordId,
          statusElement,
        }));
        card.append(editButton);
      }
      return card;
    });
    list.replaceChildren(...cards);
    if (!cards.length) list.append(makeElement("p", "service-empty", "No upcoming recordings."));
    statusElement.textContent = "Schedule loaded from MythTV.";
  } catch (error) {
    summary.textContent = "";
    statusElement.textContent = error.message;
    list.replaceChildren(makeElement("p", "service-empty", "Could not load upcoming recordings."));
  }
}

function activateView(name) {
  if (name !== "guide") closeGuideDetails();
  for (const button of viewButtons) {
    const selected = button.dataset.view === name;
    button.classList.toggle("active", selected);
    if (selected) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  }
  for (const panel of viewPanels) panel.hidden = panel.dataset.viewPanel !== name;
  const selectedButton = viewButtons.find((button) => button.dataset.view === name);
  document.title = `${selectedButton?.textContent.trim() || "Recordings"} | MythTV`;
  if (name === "recordings") loadGroups();
  if (name === "videos") loadVideos();
  if (name === "live") loadGuide(true);
  if (name === "upcoming") {
    const upcomingButton = viewButtons.find((button) => button.dataset.view === "upcoming");
    upcomingButton.dataset.loaded = "true";
    loadUpcoming();
  }
  if (name === "guide") loadGuide();
  if (name === "status") loadStatus();
  window.history.replaceState(null, "", `${window.location.pathname}#${name}`);
}

if (viewButtons.length) {
  for (const button of viewButtons) {
    button.addEventListener("click", () => activateView(button.dataset.view));
  }
  document.querySelector("#refresh-live").addEventListener("click", () => loadGuide(true));
  document.querySelector("#refresh-upcoming").addEventListener("click", loadUpcoming);
  document.querySelector("#refresh-status").addEventListener("click", loadStatus);
  document.querySelector("#guide-previous").addEventListener("click", () => {
    guideStartTime.setHours(guideStartTime.getHours() - 4);
    loadGuide();
  });
  document.querySelector("#guide-now").addEventListener("click", () => {
    guideStartTime.setTime(Date.now());
    guideStartTime.setMinutes(0, 0, 0);
    loadGuide();
  });
  document.querySelector("#guide-next").addEventListener("click", () => {
    guideStartTime.setHours(guideStartTime.getHours() + 4);
    loadGuide();
  });
  window.setInterval(updateGuideNowIndicator, 60000);

  const initialView = window.location.hash.slice(1);
  if (viewButtons.some((button) => button.dataset.view === initialView)) {
    activateView(initialView);
  } else {
    loadGroups();
  }
} else {
  loadGroups();
}
