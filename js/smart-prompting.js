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
		return (await response.json()).hash ?? null;
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
		if (typeof api.dispatchCustomEvent === "function") {
			api.dispatchCustomEvent("autoQueueGraphChanged");
		} else {
			api.dispatchEvent(new CustomEvent("autoQueueGraphChanged"));
		}
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
		scheduleWatch();
	},
	async nodeCreated(node) {
		if (node.comfyClass === "TextStyleSelector") {
			setupStyleSelector(node);
		}

		if (node.comfyClass === "TextCapture") {
			setupTextCapture(node);
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
