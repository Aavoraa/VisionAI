// frontend/src/App.jsx
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { BillListPage } from "./pages/BillListPage";
import { ScanBillPage } from "./pages/ScanBillPage";
import "./styles.css";

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<BillListPage />} />
        <Route path="/bills/:billId" element={<ScanBillPage />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  );
}
