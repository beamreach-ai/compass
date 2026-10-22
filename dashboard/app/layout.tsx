import '../styles/global.css';
import { PRODUCT_NAME } from "./lib/edition";

export default function RootLayout({ children }: { children: React.ReactNode }) {
  // the title says demo too: a tab or a recording never passes for someone's own account
  const title = process.env.APP_MODE === "demo" ? `${PRODUCT_NAME} · demo` : PRODUCT_NAME;
  return (
    <html lang="en">
      <head>
        <link rel="icon" href="/favicon.ico" />
        <title>{title}</title>
      </head>
      <body className="bg-gray-50 text-gray-900">{children}</body>
    </html>
  );
}
