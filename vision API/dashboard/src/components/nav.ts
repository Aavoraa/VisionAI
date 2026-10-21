import {
  Activity,
  BarChart3,
  CreditCard,
  KeyRound,
  LayoutDashboard,
  ReceiptText,
  ScanEye,
  Settings,
  Tags,
  Users,
  type LucideIcon,
} from "lucide-react";

import type { User } from "@/lib/api/client";

export type NavItem = {
  href: string;
  label: string;
  icon: LucideIcon;
  /** Shown only to administrators (under "Administration"). The API enforces this independently. */
  adminOnly?: boolean;
};

// Pages appear here as they are built, so the navigation never links to a missing page.
export const NAV_ITEMS: NavItem[] = [
  { href: "/overview", label: "Overview", icon: LayoutDashboard },
  { href: "/keys", label: "API keys", icon: KeyRound },
  { href: "/playground", label: "Playground", icon: ScanEye },
  { href: "/invoices", label: "Invoices", icon: ReceiptText },
  { href: "/usage", label: "Usage", icon: BarChart3 },
  { href: "/billing", label: "Billing", icon: CreditCard },
  { href: "/settings", label: "Settings", icon: Settings },
  { href: "/admin/users", label: "Users", icon: Users, adminOnly: true },
  { href: "/admin/plans", label: "Plans", icon: Tags, adminOnly: true },
  { href: "/admin/usage", label: "Platform usage", icon: Activity, adminOnly: true },
];

export function navItemsFor(user: Pick<User, "role">, items: NavItem[] = NAV_ITEMS): NavItem[] {
  return items.filter((item) => !item.adminOnly || user.role === "admin");
}

export function isActive(pathname: string, href: string): boolean {
  return pathname === href || pathname.startsWith(`${href}/`);
}
