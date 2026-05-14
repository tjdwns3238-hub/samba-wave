"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import SambaModal from "@/components/samba/Modal";
import SambaBlockAlert from "@/components/samba/BlockAlert";
import type { SambaUser } from "@/lib/samba/api/operations";
import { STORAGE_KEYS } from "@/lib/samba/constants";
import { attachDeviceIdListener } from "@/lib/samba/deviceId";
import { orderApi } from "@/lib/samba/api/commerce";
import { fmtNum } from "@/lib/samba/styles";

interface NavItem {
  href: string;
  label: string;
  planned?: boolean;
  children?: { href: string; label: string }[];
}

const NAV_ITEMS: NavItem[] = [
  { href: "/samba/collector", label: "상품수집" },
  { href: "/samba/products", label: "상품관리" },
  { href: "/samba/manual-products", label: "수동등록", planned: true },
  { href: "/samba/policies", label: "정책관리" },
  { href: "/samba/categories", label: "카테고리매핑" },
  { href: "/samba/shipments", label: "상품전송/삭제" },
  { href: "/samba/warroom", label: "오토튠" },
  { href: "/samba/store-care", label: "스토어케어", planned: true },
  { href: "/samba/sns", label: "SNS마케팅", planned: true },
  { href: "/samba/orders", label: "주문" },
  { href: "/samba/returns", label: "반품교환" },
  { href: "/samba/cs", label: "CS" },
  { href: "/samba/analytics", label: "매출통계" },
  { href: "/samba/settings", label: "설정" },
];

export default function SambaLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const pathname = usePathname();
  const router = useRouter();
  const [openDropdown, setOpenDropdown] = useState<string | null>(null);
  const [currentUser, setCurrentUser] = useState<SambaUser | null>(null);
  const [authChecked, setAuthChecked] = useState(false);

  // 로그인/회원가입/라이선스 페이지는 인증 체크 없이 렌더링
  const isLoginPage = pathname === "/samba/login" || pathname === "/samba/sign-up";
  const isLicensePage = pathname === "/samba/license";

  useEffect(() => {
    if (isLoginPage || isLicensePage) {
      setAuthChecked(true);
      return;
    }
    const raw = localStorage.getItem(STORAGE_KEYS.SAMBA_USER);
    if (raw) {
      try {
        const user = JSON.parse(raw) as SambaUser;
        setCurrentUser(user);
        // 라이선스 체크 임시 비활성화 (복구 시 아래 주석 해제)
        // if (!user.is_admin && !getLicenseKey()) {
        //   router.replace("/samba/license");
        //   return;
        // }
      } catch {
        localStorage.removeItem(STORAGE_KEYS.SAMBA_USER);
        router.replace("/samba/login");
      }
    } else {
      router.replace("/samba/login");
    }
    setAuthChecked(true);
  }, [isLoginPage, isLicensePage, router]);

  // 확장앱이 보내는 deviceId postMessage를 세션 내내 수신
  // → 오토튠 시작 시 이 deviceId를 백엔드에 전달해 "이 브라우저"에만 탭이 열리게 한다.
  useEffect(() => {
    return attachDeviceIdListener();
  }, []);

  // 글로벌 취소요청 폴링 — 주문 페이지 닫혀있어도 동작.
  // 알람 설정(주기/영업시간)을 따르되, 0→>0 으로 변하는 순간만 빨간 모달 강제 노출.
  // 카운트가 1 이상이면 사이드바 "주문" 메뉴 옆에 빨간 뱃지 항시 표시.
  const [cancelCount, setCancelCount] = useState(0);
  const [showCancelModal, setShowCancelModal] = useState(false);
  const prevCountRef = useRef(0);

  useEffect(() => {
    if (!authChecked || !currentUser) return;
    if (isLoginPage || isLicensePage) return;

    let cancelled = false;
    let intervalId: number | null = null;

    const inBusinessHours = (start: string, end: string): boolean => {
      // start = 영업 시작 (HH:MM), end = 영업 종료 (HH:MM)
      // start <= end 면 일반 케이스 (07:00 ~ 23:00 처럼 같은 날 안에서 끝남)
      // start > end 면 야간 케이스 (예: 22:00 ~ 06:00)
      try {
        const now = new Date();
        const cur = now.getHours() * 60 + now.getMinutes();
        const [sh, sm] = start.split(":").map(Number);
        const [eh, em] = end.split(":").map(Number);
        const startMin = sh * 60 + sm;
        const endMin = eh * 60 + em;
        if (Number.isNaN(startMin) || Number.isNaN(endMin)) return true;
        if (startMin === endMin) return true; // 동일 시각이면 24시간 운영으로 간주
        if (startMin < endMin) return cur >= startMin && cur < endMin;
        return cur >= startMin || cur < endMin;
      } catch {
        return true;
      }
    };

    let curSettings: { hour: number; min: number; sleep_start: string; sleep_end: string } | null = null;

    const pollOnce = async (force = false) => {
      if (cancelled || !curSettings) return;
      // sleep_end = 영업 시작, sleep_start = 영업 종료 (모달 입력 라벨 기준)
      // force=true 면 페이지 진입·설정 변경 등 사용자 액션이라 영업시간 무관 호출.
      if (!force && !inBusinessHours(curSettings.sleep_end, curSettings.sleep_start)) return;
      try {
        const { count } = await orderApi.getCancelAlertCount();
        if (cancelled) return;
        const prev = prevCountRef.current;
        prevCountRef.current = count;
        setCancelCount(count);
        // 0 → 1+ 로 새로 발생한 순간만 모달 강제 노출 (피로도 방지)
        if (count > 0 && prev === 0) setShowCancelModal(true);
      } catch {}
    };

    const start = async () => {
      try {
        const settings = await orderApi.getAlarmSettings();
        if (cancelled) return;
        curSettings = settings;
        // 페이지 진입·설정 변경 직후 1회는 영업시간 무관 강제 호출.
        // 사고 위험은 시간 안 따지므로 인지는 항상 가능해야 한다.
        await pollOnce(true);
        const intervalMs = (Number(settings.hour) * 3600 + Number(settings.min) * 60) * 1000;
        // 0초 설정이면 반복 폴링 안 함, 30초 미만이면 부하 보호로 30초로 보정.
        // 반복 폴링은 영업시간을 따른다 (force=false 기본).
        if (intervalMs > 0) {
          intervalId = window.setInterval(() => pollOnce(false), Math.max(intervalMs, 30_000));
        }
      } catch {}
    };
    start();

    const onUpdate = () => {
      if (intervalId !== null) {
        window.clearInterval(intervalId);
        intervalId = null;
      }
      start();
    };
    window.addEventListener("alarm-settings-updated", onUpdate);

    return () => {
      cancelled = true;
      if (intervalId !== null) window.clearInterval(intervalId);
      window.removeEventListener("alarm-settings-updated", onUpdate);
    };
  }, [authChecked, currentUser, isLoginPage, isLicensePage]);

  // 로그인/라이선스 페이지는 레이아웃 헤더 없이 바로 렌더링
  if (isLoginPage || isLicensePage) {
    return <>{children}</>;
  }

  // 인증 체크 중이거나 미로그인이면 빈 화면 (리다이렉트 중)
  if (!authChecked || !currentUser) {
    return (
      <div className="flex items-center justify-center min-h-screen" style={{ background: "#0F0F0F" }}>
        <p style={{ color: "#555", fontSize: "0.875rem" }}>로딩 중...</p>
      </div>
    );
  }

  const handleLogout = () => {
    localStorage.removeItem(STORAGE_KEYS.SAMBA_USER);
    // 인증 쿠키 제거
    document.cookie = "samba_user=; path=/; max-age=0";
    router.replace("/samba/login");
  };

  return (
    <div className="flex flex-col min-h-screen" style={{ background: "#0F0F0F", color: "#E5E5E5" }}>
      {/* Header */}
      <header
        className="sticky top-0 z-30"
        style={{
          background: "rgba(15,15,15,0.9)",
          borderBottom: "1px solid #2D2D2D",
          backdropFilter: "blur(4px)",
        }}
      >
        <div className="flex items-center justify-between px-8 py-3">
          {/* Logo */}
          <Link href="/samba" className="flex items-center gap-2 select-none" title="대시보드로 이동">
            <img src="/logo.png" alt="SAMBA WAVE Logo" width={40} height={40} className="object-contain" style={{ borderRadius: "8.8px" }} />
            <div>
              <h1 style={{ fontSize: "0.9375rem", fontWeight: 800, color: "#E5E5E5", letterSpacing: "0.08em", lineHeight: 1.1, textTransform: "uppercase" }}>
                SAMBA WAVE
              </h1>
              <p style={{ fontSize: "0.5625rem", color: "#666", letterSpacing: "0.04em", lineHeight: 1 }}>
                무재고 위탁판매 솔루션
              </p>
            </div>
          </Link>

          {/* Navigation */}
          <nav className="flex items-stretch ml-12" style={{ gap: 0 }}>
            {NAV_ITEMS.map((item) => {
              if (item.children) {
                // Dropdown
                const isGroupActive = item.children.some((c) => pathname.startsWith(c.href));
                return (
                  <div
                    key={item.label}
                    className="relative"
                    onMouseEnter={() => setOpenDropdown(item.label)}
                    onMouseLeave={() => setOpenDropdown(null)}
                  >
                    <button
                      className="flex items-center gap-1"
                      style={{
                        padding: "0.75rem 1.5rem",
                        fontSize: "0.875rem",
                        fontWeight: 500,
                        color: isGroupActive ? "#FF8C00" : "#E5E5E5",
                        background: "transparent",
                        borderTop: "none",
                        borderLeft: "none",
                        borderRight: "none",
                        borderBottomWidth: "2px",
                        borderBottomStyle: "solid",
                        borderBottomColor: isGroupActive ? "#FF8C00" : "transparent",
                        cursor: "pointer",
                        transition: "color 0.15s, border-color 0.15s",
                      }}
                      onMouseEnter={(e) => { e.currentTarget.style.color = "#FF8C00"; e.currentTarget.style.borderBottomColor = "#FF8C00"; }}
                      onMouseLeave={(e) => {
                        if (!isGroupActive) { e.currentTarget.style.color = "#E5E5E5"; e.currentTarget.style.borderBottomColor = "transparent"; }
                      }}
                    >
                      {item.label} <span style={{ fontSize: "0.625rem", transition: "transform 0.2s", transform: openDropdown === item.label ? "rotate(180deg)" : "none" }}>▼</span>
                    </button>
                    {openDropdown === item.label && (
                      <div
                        style={{
                          position: "absolute",
                          top: "calc(100% + 1px)",
                          left: 0,
                          background: "#1A1A1A",
                          border: "1px solid #2D2D2D",
                          borderRadius: "8px",
                          minWidth: "200px",
                          zIndex: 40,
                          boxShadow: "0 8px 32px rgba(0,0,0,0.5)",
                          overflow: "hidden",
                        }}
                      >
                        {item.children.map((child) => {
                          const isChildActive = pathname === child.href;
                          return (
                            <Link
                              key={child.href}
                              href={child.href}
                              style={{
                                display: "block",
                                padding: "0.625rem 1.25rem",
                                color: isChildActive ? "#FF8C00" : "#C5C5C5",
                                fontSize: "0.8125rem",
                                background: isChildActive ? "rgba(255,140,0,0.12)" : "transparent",
                                transition: "color 0.15s, border-color 0.15s, background 0.15s",
                              }}
                              onMouseEnter={(e) => { e.currentTarget.style.color = "#FF8C00"; e.currentTarget.style.background = "rgba(255,140,0,0.08)"; }}
                              onMouseLeave={(e) => {
                                if (!isChildActive) { e.currentTarget.style.color = "#C5C5C5"; e.currentTarget.style.background = "transparent"; }
                              }}
                            >
                              {child.label}
                            </Link>
                          );
                        })}
                      </div>
                    )}
                  </div>
                );
              }

              // Single nav item
              const isActive =
                item.href === "/samba/products"
                  ? pathname === "/samba/products"
                  : pathname.startsWith(item.href);
              const isOrdersTab = item.href === "/samba/orders";
              return (
                <div key={item.href} className="relative">
                  <Link
                    href={item.href}
                    style={{
                      display: "flex",
                      flexDirection: "column",
                      alignItems: "center",
                      justifyContent: "center",
                      gap: item.planned ? "0.1rem" : 0,
                      minHeight: "44px",
                      padding: item.planned ? "0.35rem 1.5rem" : "0.75rem 1.5rem",
                      fontSize: "0.875rem",
                      fontWeight: 500,
                      color: isActive ? "#FF8C00" : "#E5E5E5",
                      lineHeight: 1.1,
                      borderBottom: `2px solid ${isActive ? "#FF8C00" : "transparent"}`,
                      transition: "color 0.15s, border-color 0.15s",
                      position: "relative",
                    }}
                    onMouseEnter={(e) => { e.currentTarget.style.color = "#FF8C00"; e.currentTarget.style.borderBottomColor = "#FF8C00"; }}
                    onMouseLeave={(e) => {
                      if (!isActive) { e.currentTarget.style.color = "#E5E5E5"; e.currentTarget.style.borderBottomColor = "transparent"; }
                    }}
                  >
                    <span>{item.label}</span>
                    {item.planned && (
                      <span style={{ fontSize: "0.625rem", color: "#777", fontWeight: 500 }}>
                        (개발예정)
                      </span>
                    )}
                    {isOrdersTab && cancelCount > 0 && (
                      <span
                        title={`미처리 취소요청 ${fmtNum(cancelCount)}건`}
                        style={{
                          position: "absolute",
                          top: "4px",
                          right: "6px",
                          minWidth: "18px",
                          height: "18px",
                          padding: "0 5px",
                          background: "#FF4444",
                          color: "#fff",
                          fontSize: "0.6875rem",
                          fontWeight: 700,
                          borderRadius: "9px",
                          display: "flex",
                          alignItems: "center",
                          justifyContent: "center",
                          boxShadow: "0 0 0 2px #0F0F0F",
                          lineHeight: 1,
                          animation: "samba-cancel-pulse 1.6s ease-in-out infinite",
                        }}
                      >
                        {cancelCount > 99 ? "99+" : fmtNum(cancelCount)}
                      </span>
                    )}
                  </Link>
                </div>
              );
            })}
          </nav>

          {/* 알림 + 계정관리 + 사용자 정보 + 로그아웃 */}
          <div className="flex items-center gap-3">
            {/* 취소 알림 설정 */}
            <button
              title="취소 알림 설정"
              onClick={() => {
                if (pathname.startsWith("/samba/orders")) {
                  window.dispatchEvent(new CustomEvent("open-alarm-setting"))
                } else {
                  router.push("/samba/orders?alarm=1")
                }
              }}
              style={{
                width: "32px",
                height: "32px",
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                background: "transparent",
                border: "none",
                borderRadius: "6px",
                cursor: "pointer",
                color: "#FFD93D",
                fontSize: "1.125rem",
                transition: "color 0.15s, border-color 0.15s, background 0.15s",
                position: "relative",
              }}
              onMouseEnter={(e) => { e.currentTarget.style.color = "#FF8C00"; e.currentTarget.style.background = "rgba(255,140,0,0.08)"; }}
              onMouseLeave={(e) => { e.currentTarget.style.color = "#FFD93D"; e.currentTarget.style.background = "transparent"; }}
            >
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9" />
                <path d="M13.73 21a2 2 0 0 1-3.46 0" />
              </svg>
            </button>
            {/* 계정관리 아이콘 */}
            <Link
              href="/samba/users"
              title="계정관리"
              style={{
                width: "32px",
                height: "32px",
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                background: pathname.startsWith("/samba/users") ? "rgba(255,140,0,0.12)" : "transparent",
                border: "none",
                borderRadius: "6px",
                cursor: "pointer",
                color: pathname.startsWith("/samba/users") ? "#FF8C00" : "#888",
                fontSize: "1.125rem",
                transition: "color 0.15s, border-color 0.15s, background 0.15s",
              }}
              onMouseEnter={(e) => { e.currentTarget.style.color = "#FF8C00"; e.currentTarget.style.background = "rgba(255,140,0,0.08)"; }}
              onMouseLeave={(e) => {
                if (!pathname.startsWith("/samba/users")) { e.currentTarget.style.color = "#888"; e.currentTarget.style.background = "transparent"; }
              }}
            >
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2" />
                <circle cx="12" cy="7" r="4" />
              </svg>
            </Link>
            {/* 구분선 */}
            <div style={{ width: "1px", height: "20px", background: "#333" }} />
            <span style={{ fontSize: "0.8125rem", color: "#AAA" }}>
              {currentUser.name || currentUser.email}
            </span>
            <button
              onClick={handleLogout}
              style={{
                padding: "0.375rem 0.75rem",
                fontSize: "0.75rem",
                color: "#888",
                background: "transparent",
                border: "1px solid #333",
                borderRadius: "6px",
                cursor: "pointer",
                transition: "color 0.15s, border-color 0.15s, background 0.15s",
              }}
              onMouseEnter={(e) => { e.currentTarget.style.color = "#FF6B6B"; e.currentTarget.style.borderColor = "#FF6B6B"; }}
              onMouseLeave={(e) => { e.currentTarget.style.color = "#888"; e.currentTarget.style.borderColor = "#333"; }}
            >
              로그아웃
            </button>
          </div>
        </div>
      </header>

      {/* 소싱처 접속 차단 알림 배너 */}
      <SambaBlockAlert />

      {/* Main content - full width, gradient background */}
      <main
        className="flex-1"
        style={{
          background: "linear-gradient(135deg, #0F0F0F 0%, #1A1A1A 100%)",
          paddingTop: "2rem",
        }}
      >
        <div style={{ padding: "0 3rem 4rem 3rem", maxWidth: "1600px", margin: "0 auto", width: "100%" }}>
          {children}
        </div>
      </main>
      <SambaModal />

      {/* 취소요청 새로 발생 시 강제 노출 모달 — 0→1+ 변화 순간만 뜬다 */}
      {showCancelModal && cancelCount > 0 && (
        <div
          style={{
            position: "fixed",
            inset: 0,
            background: "rgba(0,0,0,0.75)",
            backdropFilter: "blur(4px)",
            zIndex: 9999,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
          }}
        >
          <div
            style={{
              background: "#1A1A1A",
              border: "2px solid #FF4444",
              borderRadius: "16px",
              padding: "2rem",
              maxWidth: "440px",
              width: "90%",
              boxShadow: "0 8px 32px rgba(255,68,68,0.3)",
              position: "relative",
            }}
          >
            {/* X 닫기 (우측 상단) — 모달 닫고 디폴트 오늘 주문 화면으로 복귀 */}
            <button
              aria-label="알람 닫기"
              title="닫기 (디폴트 화면으로 이동)"
              onClick={() => {
                setShowCancelModal(false);
                window.dispatchEvent(new CustomEvent("reset-orders-filter"));
                router.push("/samba/orders");
              }}
              style={{
                position: "absolute",
                top: "0.75rem",
                right: "0.75rem",
                width: "28px",
                height: "28px",
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                background: "transparent",
                border: "none",
                borderRadius: "6px",
                color: "#AAA",
                fontSize: "1.25rem",
                fontWeight: 700,
                cursor: "pointer",
                lineHeight: 1,
              }}
              onMouseEnter={(e) => { e.currentTarget.style.color = "#FF6B6B"; e.currentTarget.style.background = "rgba(255,107,107,0.1)"; }}
              onMouseLeave={(e) => { e.currentTarget.style.color = "#AAA"; e.currentTarget.style.background = "transparent"; }}
            >
              ✕
            </button>
            <div style={{ textAlign: "center", marginBottom: "1.5rem" }}>
              <div style={{ fontSize: "3rem", marginBottom: "0.75rem" }}>⚠️</div>
              <h3 style={{ fontSize: "1.25rem", fontWeight: 700, color: "#FF6B6B", marginBottom: "0.5rem" }}>
                주문 취소요청 감지
              </h3>
              <p style={{ fontSize: "0.875rem", color: "#AAA", lineHeight: 1.5 }}>
                고객이 취소요청한 주문이 <b style={{ color: "#FF6B6B" }}>{fmtNum(cancelCount)}건</b>{" "}
                있습니다. 발주·송장 등록 전에 확인해 주세요.
              </p>
            </div>
            <div style={{ display: "flex", gap: "0.5rem" }}>
              <button
                onClick={() => setShowCancelModal(false)}
                style={{
                  flex: 1,
                  padding: "0.75rem",
                  background: "transparent",
                  border: "1px solid #444",
                  borderRadius: "8px",
                  color: "#AAA",
                  fontSize: "0.9375rem",
                  fontWeight: 600,
                  cursor: "pointer",
                }}
              >
                나중에
              </button>
              <button
                onClick={() => {
                  setShowCancelModal(false);
                  router.push("/samba/orders?cancel_alert=1");
                }}
                style={{
                  flex: 2,
                  padding: "0.75rem",
                  background: "#FF4444",
                  border: "none",
                  borderRadius: "8px",
                  color: "#fff",
                  fontSize: "0.9375rem",
                  fontWeight: 700,
                  cursor: "pointer",
                }}
              >
                지금 확인하기
              </button>
            </div>
          </div>
        </div>
      )}

      <style jsx global>{`
        @keyframes samba-cancel-pulse {
          0%, 100% { box-shadow: 0 0 0 2px #0F0F0F, 0 0 0 0 rgba(255, 68, 68, 0.6); }
          50% { box-shadow: 0 0 0 2px #0F0F0F, 0 0 0 6px rgba(255, 68, 68, 0); }
        }
      `}</style>
    </div>
  );
}
