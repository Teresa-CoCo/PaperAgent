import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import "@tinymomentum/liquid-glass-react/dist/components/LiquidGlassBase.css";
import "./styles/app.css";

ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
