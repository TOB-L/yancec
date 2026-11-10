import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App, { ErrorBoundary } from "./App.jsx";

// 最外层错误边界：任何错误（含 App 自身渲染崩溃）都显示可见的崩溃页，杜绝"白屏不知错在哪"
createRoot(document.getElementById("root")).render(
  <ErrorBoundary>
    <StrictMode>
      <App />
    </StrictMode>
  </ErrorBoundary>
);
