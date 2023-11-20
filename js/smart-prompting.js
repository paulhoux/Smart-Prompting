import { app } from "../../scripts/app.js";
import { addValueControlWidget } from "../../scripts/widgets.js";

app.registerExtension({
	name: "smart-prompting",
	async nodeCreated(node) {
        const hairColorWidgetIndex = node.widgets?.findIndex((w) => w.name === "color"); 
        if (hairColorWidgetIndex > -1) {
            const hairColorWidget = node.widgets[hairColorWidgetIndex];
            const hairColorValueControl = addValueControlWidget(node, hairColorWidget, "fixed");
            node.widgets.splice(hairColorWidgetIndex+1,0,node.widgets.pop());
        }
        
        const hairStyleWidgetIndex = node.widgets?.findIndex((w) => w.name === "style"); 
        if (hairStyleWidgetIndex > -1) {
            const hairStyleWidget = node.widgets[hairStyleWidgetIndex];
            const hairStyleValueControl = addValueControlWidget(node, hairStyleWidget, "fixed");
            node.widgets.splice(hairStyleWidgetIndex+1,0,node.widgets.pop());
        }
        
        const cameraWidgetIndex = node.widgets?.findIndex((w) => w.name === "camera"); 
        if (cameraWidgetIndex > -1) {
            const cameraWidget = node.widgets[cameraWidgetIndex];
            const cameraValueControl = addValueControlWidget(node, cameraWidget, "fixed");
            node.widgets.splice(cameraWidgetIndex+1,0,node.widgets.pop());
        }
        
        const ethnicWidgetIndex = node.widgets?.findIndex((w) => w.name === "ethnic"); 
        if (ethnicWidgetIndex > -1) {
            const ethnicWidget = node.widgets[ethnicWidgetIndex];
            const ethnicValueControl = addValueControlWidget(node, ethnicWidget, "fixed");
            node.widgets.splice(ethnicWidgetIndex+1,0,node.widgets.pop());
        }
    }
});