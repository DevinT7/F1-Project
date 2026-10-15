import type { Metadata, Viewport } from "next";
import type { ReactNode } from "react";

// Titillium Web, self-hosted (no build-time font download): body, bold UI and black-italic display
import "@fontsource/titillium-web/400.css";
import "@fontsource/titillium-web/600.css";
import "@fontsource/titillium-web/700.css";
import "@fontsource/titillium-web/700-italic.css";
import "@fontsource/titillium-web/900.css";
import "./globals.css";

export const metadata: Metadata = {
  title: "Senna · Pit Wall",
  description: "Distance-aligned F1 lap comparison: telemetry, time delta, track map, tyres and an AI race engineer.",
};

export const viewport: Viewport = { width: "device-width", initialScale: 1, themeColor: "#07070a" };

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
