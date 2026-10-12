import type { Metadata, Viewport } from "next";
import type { ReactNode } from "react";

import "./globals.css";

export const metadata: Metadata = {
  title: "RaceIQ · Pit Wall",
  description: "Distance-aligned F1 lap comparison: telemetry, time delta, track map and corner findings.",
};

export const viewport: Viewport = { width: "device-width", initialScale: 1 };

// Applies a saved light-theme preference before first paint (dark is the default).
const themeScript = `try{var t=localStorage.getItem("raceiq-theme");if(t==="light"||t==="dark")document.documentElement.dataset.theme=t}catch(e){}`;

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en" data-theme="dark" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: themeScript }} />
      </head>
      <body>{children}</body>
    </html>
  );
}
