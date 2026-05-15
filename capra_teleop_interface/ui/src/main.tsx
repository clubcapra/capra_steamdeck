import "@blueprintjs/core/lib/css/blueprint.css";
import "@blueprintjs/icons/lib/css/blueprint-icons.css";
import "./styles.css";

import { FocusStyleManager } from "@blueprintjs/core";
import React from "react";
import ReactDOM from "react-dom/client";

import { App } from "./App";

// Only show focus rings when the operator is actually using the keyboard.
FocusStyleManager.onlyShowFocusOnTabs();

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
