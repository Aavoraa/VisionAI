import { redirect } from "next/navigation";

// The proxy has already sent anonymous visitors to /login.
export default function Home(): never {
  redirect("/overview");
}
