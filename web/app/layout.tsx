import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "PresyoWatch — Philippine commodity prices",
  description:
    "Daily retail prices for agricultural and fishery commodities in the Philippines, " +
    "from the Department of Agriculture's Bantay Presyo monitoring sheets.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
