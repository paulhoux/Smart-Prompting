import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";
import { addValueControlWidget } from "../../scripts/widgets.js";

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

app.registerExtension({
	name: "smart-prompting",
	async nodeCreated(node) {
		if (node.comfyClass === "TextStyleSelector") {
			setupStyleSelector(node);
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
