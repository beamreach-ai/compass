// Which build this is. The open edition (NEXT_PUBLIC_EDITION=open, set when the
// image is built) has no licence, billing, founder contact or Beamreach logo;
// the product build is unchanged.
export const isOpenEdition = process.env.NEXT_PUBLIC_EDITION === "open";
export const PRODUCT_NAME = isOpenEdition ? "Compass" : "Beamreach Compass";
export const COMPANY_NAME = isOpenEdition ? "Compass" : "Beamreach";
export const LOGO_URL: string | null = isOpenEdition
  ? null
  : "https://s3.us-east-1.amazonaws.com/beamreach.cloud/assets/images/beamreach-logo.png";
// The "talk to the founder" block in the sidebar: a WhatsApp number (digits) and an
// email, given to the product build as NEXT_PUBLIC_SUPPORT_*; nothing in the open edition.
export const SUPPORT_WHATSAPP: string | null = isOpenEdition ? null : process.env.NEXT_PUBLIC_SUPPORT_WHATSAPP || null;
export const SUPPORT_EMAIL: string | null = isOpenEdition ? null : process.env.NEXT_PUBLIC_SUPPORT_EMAIL || null;
