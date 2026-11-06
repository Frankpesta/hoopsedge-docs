"use client";

import { useEffect, useState } from "react";

export function ThemeToggle() {
  const [light, setLight] = useState(false);

  useEffect(() => {
    setLight(document.documentElement.dataset.theme === "light");
  }, []);

  function toggle() {
    const next = !light;
    setLight(next);
    if (next) document.documentElement.dataset.theme = "light";
    else delete document.documentElement.dataset.theme;
    try {
      localStorage.setItem("theme", next ? "light" : "dark");
    } catch {
      // Private browsing can refuse storage. The theme still changes for this page.
    }
  }

  return (
    <button
      type="button"
      onClick={toggle}
      className="text-text-2 border-line min-h-11 rounded-md border px-3 text-sm"
    >
      {light ? "Dark" : "Light"}
    </button>
  );
}
