import { useEffect, useState } from "react";
import { AppShell } from "@/components/AppShell";
import type { TabId } from "@/components/TabBar";
import { LeadsScreen } from "@/features/leads/LeadsScreen";
import { LeadCard } from "@/features/lead/LeadCard";
import { ClientsScreen } from "@/features/clients/ClientsScreen";
import { StatsScreen } from "@/features/stats/StatsScreen";
import { EventScreen } from "@/features/event/EventScreen";
import { TestChatScreen } from "@/features/testchat/TestChatScreen";
import { initTelegram, applyColorScheme } from "@/lib/telegram";

export default function App() {
  const [tab, setTab] = useState<TabId>("leads");
  const [openPhone, setOpenPhone] = useState<string | null>(null);

  useEffect(() => {
    initTelegram(); // принудительная init SDK (в dev тихо уходит в fallback)
    applyColorScheme();
  }, []);

  const openLead = (phone: string) => setOpenPhone(phone);

  // Карточка лида — поверх всего, на весь экран (mobile-навигация push/back).
  if (openPhone) {
    return <LeadCard phone={openPhone} onBack={() => setOpenPhone(null)} />;
  }

  return (
    <AppShell active={tab} onTabChange={setTab}>
      {tab === "leads" && <LeadsScreen onOpenLead={openLead} />}
      {tab === "clients" && <ClientsScreen />}
      {tab === "stats" && <StatsScreen onOpenLead={openLead} />}
      {tab === "event" && <EventScreen />}
      {tab === "test" && <TestChatScreen />}
    </AppShell>
  );
}
