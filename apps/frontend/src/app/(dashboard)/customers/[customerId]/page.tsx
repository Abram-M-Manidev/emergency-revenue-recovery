"use client";

import { useParams } from "next/navigation";

import { CustomerDetail } from "@/components/customers/customer-detail";

export default function CustomerDetailPage() {
  const params = useParams<{ customerId: string }>();
  return <CustomerDetail customerId={params.customerId} />;
}
