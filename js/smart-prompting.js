import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";
import { addValueControlWidget, ComfyWidgets } from "../../scripts/widgets.js";

// Cached {all, categories} payload from /smart-prompting/styles
let styleCatalogPromise = null;

function getStyleCatalog() {
	if (!styleCatalogPromise) {
		styleCatalogPromise = api
			.fetchApi("/smart-prompting/styles")
			.then((response) => response.json())
			.catch((error) => {
				console.warn("[smart-prompting] Could not load style categories:", error);
				return null;
			});
	}
	return styleCatalogPromise;
}

const CONTROL_VALUES = new Set(["fixed", "increment", "decrement", "randomize"]);

// Older versions accidentally gave the Text Style Selector's 'style' widget a
// hidden "control after generate" widget (meant for the character selector's
// hair style). Workflows saved back then store its value right after the
// style, which would now land in 'category'. Shift it back on load.
function repairLegacyStyleSelectorValues(node) {
	const onConfigure = node.onConfigure;
	node.onConfigure = function (info) {
		const result = onConfigure?.apply(this, arguments);
		const saved = info?.widgets_values;
		const categoryWidget = this.widgets?.find((w) => w.name === "category");
		const categories = categoryWidget?.options?.values ?? [];
		if (
			categoryWidget &&
			Array.isArray(saved) &&
			CONTROL_VALUES.has(saved[1]) &&
			!categories.includes(categoryWidget.value)
		) {
			categoryWidget.value = categories.includes(saved[2]) ? saved[2] : categories[0];
		}
		return result;
	};
}

// Restricts the 'style' combo to the styles of the selected category.
async function setupStyleSelector(node) {
	const categoryWidget = node.widgets?.find((w) => w.name === "category");
	const styleWidget = node.widgets?.find((w) => w.name === "style");
	if (!categoryWidget || !styleWidget) {
		return;
	}

	const catalog = await getStyleCatalog();
	if (!catalog?.categories) {
		return;
	}

	const allLabel = catalog.all ?? "all";
	const everyStyle = [...(styleWidget.options?.values ?? [])];

	const applyFilter = (categoryName, keepValue) => {
		const names =
			categoryName === allLabel ? everyStyle : catalog.categories[categoryName] ?? everyStyle;

		styleWidget.options = styleWidget.options ?? {};
		styleWidget.options.values = names;

		// Keep the current style if it is still in the list, otherwise take the first.
		if (!keepValue || !names.includes(styleWidget.value)) {
			styleWidget.value = names[0];
			styleWidget.callback?.(styleWidget.value);
		}
		node.setDirtyCanvas?.(true, true);
	};

	const originalCallback = categoryWidget.callback;
	categoryWidget.callback = function (value, ...args) {
		const result = originalCallback?.apply(this, [value, ...args]);
		applyFilter(value, false);
		return result;
	};

	// Apply once on load without discarding the saved style.
	applyFilter(categoryWidget.value, true);
}

// ---------------------------------------------------------------------------
// Text File disk watcher
//
// The frontend's "Run (on change)" works by diffing the serialized graph and
// dispatching 'autoQueueGraphChanged' when the execution-relevant state moved.
// It never looks at the filesystem, so editing a watched text file is invisible
// to it. This polls the file the node points at and raises that same event when
// the contents actually change. The auto queue handler still checks that the
// run mode is "change", so nothing is queued unless the user asked for it.
// ---------------------------------------------------------------------------
const WATCH_ENABLED_SETTING = "smartPrompting.watchTextFiles";
const WATCH_INTERVAL_SETTING = "smartPrompting.watchInterval";

// node id + file name -> last seen hash
const lastSeenHashes = new Map();

// file name -> hash right after Text Capture wrote to it. A change we made
// ourselves should not re-queue the graph.
const selfWrittenHashes = new Map();

// The server reports names with the OS separator, widgets may hold either.
function normalizeFileName(file) {
	return String(file ?? "").replaceAll("\\", "/");
}
let watchTimer = null;

function getSetting(id, fallback) {
	try {
		const value =
			app.extensionManager?.setting?.get(id) ?? app.ui?.settings?.getSettingValue(id);
		return value === undefined || value === null ? fallback : value;
	} catch (error) {
		return fallback;
	}
}

async function fetchFileHash(file) {
	try {
		const response = await api.fetchApi(
			`/smart-prompting/text-file-hash?file=${encodeURIComponent(file)}`
		);
		if (!response.ok) {
			return null;
		}
		const data = await response.json();
		if (Number.isInteger(data.count)) {
			textFileLineCounts.set(file, data.count);
		}
		return data.hash ?? null;
	} catch (error) {
		return null;
	}
}

async function pollWatchedFiles() {
	// Collect the file each TextFile node currently points at.
	const targets = new Map(); // file -> [key, ...]
	for (const node of app.graph?.nodes ?? []) {
		if (node.comfyClass !== "TextFile" || node.mode === 2 || node.mode === 4) {
			continue; // skip muted and bypassed nodes
		}
		const file = node.widgets?.find((w) => w.name === "file")?.value;
		if (!file) {
			continue;
		}
		if (!targets.has(file)) {
			targets.set(file, []);
		}
		targets.get(file).push(`${node.id}::${file}`);
	}

	// Forget nodes that were deleted or repointed at another file.
	const live = new Set([...targets.values()].flat());
	for (const key of lastSeenHashes.keys()) {
		if (!live.has(key)) {
			lastSeenHashes.delete(key);
		}
	}

	let changed = false;
	for (const [file, keys] of targets) {
		const hash = await fetchFileHash(file);
		if (hash === null) {
			continue;
		}
		const selfWritten = selfWrittenHashes.get(normalizeFileName(file)) === hash;
		for (const key of keys) {
			const previous = lastSeenHashes.get(key);
			lastSeenHashes.set(key, hash);
			// A first sighting establishes the baseline, it is not a change.
			if (previous !== undefined && previous !== hash && !selfWritten) {
				changed = true;
				console.log(`[smart-prompting] '${file}' changed on disk`);
			}
		}
	}

	if (changed) {
		// Same event the change tracker raises. The auto queue handler ignores
		// it unless the run mode is "change".
		signalGraphChanged();
	}
}

function scheduleWatch() {
	clearTimeout(watchTimer);
	const interval = Math.max(500, Number(getSetting(WATCH_INTERVAL_SETTING, 2000)) || 2000);
	watchTimer = setTimeout(async () => {
		if (getSetting(WATCH_ENABLED_SETTING, true) && document.visibilityState !== "hidden") {
			try {
				await pollWatchedFiles();
			} catch (error) {
				console.warn("[smart-prompting] Text file watch failed:", error);
			}
		}
		scheduleWatch();
	}, interval);
}

// ---------------------------------------------------------------------------
// Text String: only a finished edit counts as a change
//
// In "Run (on change)" mode the frontend re-queues on every keystroke in a
// text box. For Text String nodes we hold that signal back while the box has
// focus and raise it once the edit is committed: when focus leaves the box.
// Ctrl+Enter (Comfy's "Queue prompt" shortcut) already queues a run from inside
// a text box, so there we only mark the text as committed.
// ---------------------------------------------------------------------------
const AUTO_QUEUE_EVENT = "autoQueueGraphChanged";

// The frontend's own dispatcher, so our signals bypass the filter below.
let dispatchUnfiltered = null;

function signalGraphChanged() {
	if (dispatchUnfiltered) {
		dispatchUnfiltered(AUTO_QUEUE_EVENT);
	} else if (typeof api.dispatchCustomEvent === "function") {
		api.dispatchCustomEvent(AUTO_QUEUE_EVENT);
	} else {
		api.dispatchEvent(new CustomEvent(AUTO_QUEUE_EVENT));
	}
}

// node id -> text value when the current edit started
let committedTexts = new Map();

function textStringNodes() {
	return (app.graph?.nodes ?? []).filter((node) => node.comfyClass === "TextString");
}

function readText(node) {
	return node.widgets?.find((w) => w.name === "text")?.value;
}

function snapshotTextStrings() {
	committedTexts = new Map(textStringNodes().map((node) => [node.id, readText(node)]));
}

function hasUncommittedText() {
	return textStringNodes().some(
		(node) => committedTexts.has(node.id) && committedTexts.get(node.id) !== readText(node)
	);
}

function isTextField(element) {
	return (
		element instanceof HTMLElement &&
		(element.tagName === "TEXTAREA" || element.tagName === "INPUT" || element.isContentEditable)
	);
}

function setupTextStringCommit() {
	if (typeof api.dispatchCustomEvent !== "function") {
		return; // Front end too old to filter; keep its default behaviour.
	}

	dispatchUnfiltered = api.dispatchCustomEvent.bind(api);
	api.dispatchCustomEvent = function (type, ...args) {
		// Swallow re-queue requests caused by typing in a Text String box.
		// Its value is still captured, so a manual Run uses what you typed.
		if (type === AUTO_QUEUE_EVENT && isTextField(document.activeElement) && hasUncommittedText()) {
			return false;
		}
		return dispatchUnfiltered(type, ...args);
	};

	// Remember the text before any edit starts.
	document.addEventListener("focusin", (event) => {
		if (isTextField(event.target)) {
			snapshotTextStrings();
		}
	}, true);

	// Leaving the box commits the edit.
	document.addEventListener("focusout", (event) => {
		if (!isTextField(event.target)) {
			return;
		}
		const changed = hasUncommittedText();
		snapshotTextStrings();
		if (changed) {
			signalGraphChanged();
		}
	}, true);

	// Ctrl+Enter queues a run by itself; just mark the text as committed.
	window.addEventListener("keydown", (event) => {
		if (event.key === "Enter" && (event.ctrlKey || event.metaKey) && isTextField(event.target)) {
			snapshotTextStrings();
		}
	}, true);
}

// ---------------------------------------------------------------------------
// Text Capture
//
// Shows the prompt the node received on its last run and adds a "Save to file"
// button that appends it to a file in the 'text' folder. In 'auto' mode the
// server already saved it during the run; we only report the outcome.
// ---------------------------------------------------------------------------
const SAVE_LABEL = "Save to file";

function showToast(severity, detail) {
	try {
		app.extensionManager?.toast?.add({ severity, summary: "Text Capture", detail, life: 3500 });
	} catch (error) {
		// Older front ends have no toasts; the button label still reports it.
	}
}

function setupTextCapture(node) {
	const fileWidget = node.widgets?.find((w) => w.name === "file");

	// The captured prompt lives only in memory. Anything stored on the node
	// (properties, serialized widgets) is part of what "Run (on change)"
	// compares, so every new capture would queue another, empty run.
	let capturedText = "";

	// Read-only box showing what will be saved.
	let preview = null;
	try {
		preview = ComfyWidgets.STRING(node, "captured", ["STRING", { multiline: true }], app).widget;
		preview.serialize = false;
		preview.options = { ...(preview.options ?? {}), serialize: false };
		if (preview.inputEl) {
			preview.inputEl.readOnly = true;
			preview.inputEl.style.opacity = 0.7;
			preview.inputEl.placeholder = "Run the workflow to capture a prompt";
		}
	} catch (error) {
		console.warn("[smart-prompting] Could not add the capture preview:", error);
	}

	const showText = (text) => {
		capturedText = text;
		if (preview) {
			preview.value = text;
		}
		node.setDirtyCanvas?.(true, true);
	};

	let flashTimer = null;
	const button = node.addWidget("button", SAVE_LABEL, null, () => save());
	button.serialize = false;

	const flash = (label) => {
		button.label = label;
		button.name = label;
		node.setDirtyCanvas?.(true, true);
		clearTimeout(flashTimer);
		flashTimer = setTimeout(() => {
			button.label = SAVE_LABEL;
			button.name = SAVE_LABEL;
			node.setDirtyCanvas?.(true, true);
		}, 2500);
	};

	// quiet: auto mode saves on every run, so only speak up when it matters.
	const report = async (result, quiet) => {
		const file = result?.file ?? fileWidget?.value ?? "";
		if (result?.hash) {
			selfWrittenHashes.set(normalizeFileName(file), result.hash);
		}

		switch (result?.status) {
			case "saved":
				flash(`Saved (${result.count} in file)`);
				if (result.created) {
					// Make the new file show up in the Text File dropdowns.
					try {
						await app.refreshComboInNodes?.();
					} catch (error) {
						// Pressing R in ComfyUI does the same.
					}
					showToast("success", `Created ${file}`);
				} else if (!quiet) {
					showToast("success", `Added to ${file} (${result.count} prompts)`);
				}
				break;
			case "duplicate":
				flash("Already in file");
				if (!quiet) {
					showToast("info", `Already in ${file}`);
				}
				break;
			case "empty":
				flash("Nothing to save");
				if (!quiet) {
					showToast("warn", result.message || "Run the workflow first to capture a prompt");
				}
				break;
			default:
				flash("Save failed");
				showToast("error", result?.message || "Could not save the prompt");
		}
	};

	const save = async () => {
		const text = capturedText.trim();
		if (!text) {
			report({ status: "empty", message: "Run the workflow first to capture a prompt" }, false);
			return;
		}
		try {
			const response = await api.fetchApi("/smart-prompting/capture", {
				method: "POST",
				headers: { "Content-Type": "application/json" },
				body: JSON.stringify({ file: fileWidget?.value ?? "", text }),
			});
			const result = await response
				.json()
				.catch(() => ({ status: "error", message: `Server answered ${response.status}` }));
			report(result, false);
		} catch (error) {
			report({ status: "error", message: String(error) }, false);
		}
	};

	const onExecuted = node.onExecuted;
	node.onExecuted = function (message) {
		const result = onExecuted?.apply(this, arguments);
		const text = message?.text?.[0];
		if (typeof text === "string") {
			showText(text);
		}
		const saved = message?.saved?.[0];
		if (saved) {
			report(saved, true);
		}
		return result;
	};

	// Workflows saved with the first version kept the prompt in the node's
	// properties; drop it so it cannot trigger runs or go stale.
	const onConfigure = node.onConfigure;
	node.onConfigure = function () {
		const result = onConfigure?.apply(this, arguments);
		if (this.properties && "captured_text" in this.properties) {
			delete this.properties.captured_text;
		}
		return result;
	};
}

// ---------------------------------------------------------------------------
// Text File: stop at the end of the file
//
// "Run (on change)" re-queues because the seed control changes the seed after
// every run. With at_end = "stop" we hold the seed on the last line instead of
// stepping past it: nothing changes, so the auto queue goes idle by itself.
// The control stays as it is; set the seed back to 0 to run the file again
// (in "Run (on change)" mode that starts the next pass straight away).
// ---------------------------------------------------------------------------

// file name -> number of non-empty lines, as last reported by the server
const textFileLineCounts = new Map();

async function refreshLineCount(file) {
	if (!file) {
		return;
	}
	try {
		const response = await api.fetchApi(
			`/smart-prompting/text-file-hash?file=${encodeURIComponent(file)}`
		);
		if (response.ok) {
			const { count } = await response.json();
			if (Number.isInteger(count)) {
				textFileLineCounts.set(file, count);
			}
		}
	} catch (error) {
		// Unknown count: the seed is simply not held back.
	}
}

// True when stepping the seed would move past the last line of the file.
function shouldHoldSeed(node, control) {
	if (control.value !== "increment") {
		return false; // decrement stops at 0 by itself; fixed/randomize untouched
	}
	const widget = (name) => node.widgets?.find((w) => w.name === name);
	if (widget("at_end")?.value !== "stop") {
		return false;
	}
	const count = textFileLineCounts.get(widget("file")?.value);
	if (!Number.isInteger(count)) {
		return false;
	}
	const seed = Number(widget("seed")?.value ?? 0);
	const divisor = Math.max(1, Number(widget("divisor")?.value ?? 1));
	return Math.floor((seed + 1) / divisor) >= count;
}

function setupTextFileStop(node) {
	const fileWidget = node.widgets?.find((w) => w.name === "file");
	const control = node.widgets?.find((w) => w.name === "control_after_generate");
	if (!fileWidget || !control) {
		return;
	}

	// Comfy calls these around every queue; which one steps the seed depends
	// on the "Widget control mode" setting (before or after generating).
	for (const hook of ["beforeQueued", "afterQueued"]) {
		const original = control[hook];
		if (typeof original !== "function") {
			continue;
		}
		control[hook] = function (...args) {
			if (shouldHoldSeed(node, control)) {
				return; // stay on the last line
			}
			return original.apply(this, args);
		};
	}

	// Keep the line count current.
	const originalCallback = fileWidget.callback;
	fileWidget.callback = function (value, ...args) {
		refreshLineCount(value);
		return originalCallback?.apply(this, [value, ...args]);
	};
	const onExecuted = node.onExecuted;
	node.onExecuted = function (message) {
		const count = message?.item_count?.[0];
		if (Number.isInteger(count)) {
			textFileLineCounts.set(fileWidget.value, count);
		}
		return onExecuted?.apply(this, arguments);
	};
	// The saved file name is restored after nodeCreated, so look it up a bit later.
	setTimeout(() => refreshLineCount(fileWidget.value), 0);
}

// ---------------------------------------------------------------------------
// Text File: keep the line's text cached while the seed changes
//
// ComfyUI re-runs a node when any input upstream of it changes; it does not
// look at the values those nodes produce. With a divisor > 1 the seed changes
// on every run while the line stays the same, so everything fed by TEXT (an
// LLM, a text encoder) ran again for nothing.
//
// When a prompt is built we therefore:
//  - give the Text File node the first seed of the current line, so its
//    inputs only change when the line does; and
//  - hand anything wired to its SEED output the real seed directly, so a
//    sampler still gets a new seed every run.
// The workflow itself (widget values, saved files) is left untouched.
// ---------------------------------------------------------------------------
const TEXT_FILE_SEED_OUTPUT = 1; // TEXT, SEED, ITEM_COUNT, FILENAME

function isLink(value) {
	return Array.isArray(value) && value.length === 2 && typeof value[1] === "number";
}

function stabilizeTextFileSeeds(apiPrompt) {
	if (!apiPrompt || typeof apiPrompt !== "object") {
		return;
	}
	// node id -> the real seed
	const realSeeds = new Map();
	for (const [id, entry] of Object.entries(apiPrompt)) {
		if (entry?.class_type !== "TextFile") {
			continue;
		}
		const inputs = entry.inputs ?? {};
		const seed = inputs.seed;
		const divisor = inputs.divisor ?? 1;
		// Seed or divisor fed by another node: leave this one alone.
		if (!Number.isInteger(seed) || !Number.isInteger(divisor) || divisor < 1) {
			continue;
		}
		realSeeds.set(String(id), seed);
		inputs.seed = Math.floor(seed / divisor) * divisor;
	}
	if (realSeeds.size === 0) {
		return;
	}
	for (const entry of Object.values(apiPrompt)) {
		const inputs = entry?.inputs;
		if (!inputs) {
			continue;
		}
		for (const [name, value] of Object.entries(inputs)) {
			if (isLink(value) && value[1] === TEXT_FILE_SEED_OUTPUT && realSeeds.has(String(value[0]))) {
				inputs[name] = realSeeds.get(String(value[0]));
			}
		}
	}
}

function setupTextFileSeedStabilizer() {
	const original = app.graphToPrompt;
	if (typeof original !== "function") {
		return;
	}
	app.graphToPrompt = async function (...args) {
		const result = await original.apply(this, args);
		try {
			stabilizeTextFileSeeds(result?.output);
		} catch (error) {
			console.warn("[smart-prompting] Could not adjust Text File seeds:", error);
		}
		return result;
	};
}

app.registerExtension({
	name: "smart-prompting",
	settings: [
		{
			id: WATCH_ENABLED_SETTING,
			category: ["smart-prompting", "Text File", "Watch for changes"],
			name: "Re-queue when a Text File changes on disk",
			tooltip:
				"Polls the file each Text File node points at. When it changes, the graph is reported as changed so 'Run (on change)' re-queues. Has no effect in other run modes.",
			type: "boolean",
			defaultValue: true,
		},
		{
			id: WATCH_INTERVAL_SETTING,
			category: ["smart-prompting", "Text File", "Watch interval"],
			name: "Text File watch interval (ms)",
			type: "number",
			defaultValue: 2000,
			attrs: { min: 500, max: 60000, step: 500 },
		},
	],
	async setup() {
		setupTextStringCommit();
		scheduleWatch();
		setupTextFileSeedStabilizer();
	},
	async nodeCreated(node) {
		if (node.comfyClass === "TextStyleSelector") {
			repairLegacyStyleSelectorValues(node);
			setupStyleSelector(node);
			return;
		}

		if (node.comfyClass === "TextCapture") {
			setupTextCapture(node);
			return;
		}

		if (node.comfyClass === "TextFile") {
			setupTextFileStop(node);
			return;
		}

		// The value controls below belong to the Text Character Selector only;
		// other nodes can have widgets with the same names.
		if (node.comfyClass !== "TextCharacterSelector") {
			return;
		}

		const hairColorWidgetIndex = node.widgets?.findIndex((w) => w.name === "color");
		if (hairColorWidgetIndex > -1) {
			const hairColorWidget = node.widgets[hairColorWidgetIndex];
			const hairColorValueControl = addValueControlWidget(node, hairColorWidget, "fixed");
			node.widgets.splice(hairColorWidgetIndex + 1, 0, node.widgets.pop());
		}

		const hairStyleWidgetIndex = node.widgets?.findIndex((w) => w.name === "style");
		if (hairStyleWidgetIndex > -1) {
			const hairStyleWidget = node.widgets[hairStyleWidgetIndex];
			const hairStyleValueControl = addValueControlWidget(node, hairStyleWidget, "fixed");
			node.widgets.splice(hairStyleWidgetIndex + 1, 0, node.widgets.pop());
		}

		const cameraWidgetIndex = node.widgets?.findIndex((w) => w.name === "camera");
		if (cameraWidgetIndex > -1) {
			const cameraWidget = node.widgets[cameraWidgetIndex];
			const cameraValueControl = addValueControlWidget(node, cameraWidget, "fixed");
			node.widgets.splice(cameraWidgetIndex + 1, 0, node.widgets.pop());
		}

		const ethnicWidgetIndex = node.widgets?.findIndex((w) => w.name === "ethnic");
		if (ethnicWidgetIndex > -1) {
			const ethnicWidget = node.widgets[ethnicWidgetIndex];
			const ethnicValueControl = addValueControlWidget(node, ethnicWidget, "fixed");
			node.widgets.splice(ethnicWidgetIndex + 1, 0, node.widgets.pop());
		}
	},
});
