import '../styles/global.css';
import { PRODUCT_NAME } from "./lib/edition";

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <head>
        <link rel="icon" href="/favicon.ico" />
        <title>{PRODUCT_NAME}</title>
      </head>
      <body className="bg-gray-50 text-gray-900">{children}</body>
    </html>
  );
}
