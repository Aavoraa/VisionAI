import type { Metadata, Viewport } from "next";
import { Fira_Code, Fira_Sans } from "next/font/google";

import "./globals.css";

const firaSans = Fira_Sans({
  variable: "--font-fira-sans",
  subsets: ["latin"],
  weight: ["400", "500", "600", "700"],
  display: "swap",
});

const firaCode = Fira_Code({
  variable: "--font-fira-code",
  subsets: ["latin"],
  display: "swap",
});

export const metadata: Metadata = {
  title: { default: "Vision AI Console", template: "%s · Vision AI Console" },
  description: "Manage API keys, usage and billing for the Vision AI API.",
  robots: { index: false, follow: false },
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en" className={`${firaSans.variable} ${firaCode.variable} h-full`}>
      <body className="min-h-full">{children}</body>
    </html>
  );
}
