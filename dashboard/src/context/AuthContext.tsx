import React, { createContext, useContext, useState, useEffect, useCallback } from 'react';

interface AuthContextType {
  adminKey: string | null;
  setAdminKey: (key: string) => void;
  logout: () => void;
}

const AuthContext = createContext<AuthContextType | undefined>(undefined);

export const AuthProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const [adminKey, setAdminKeyState] = useState<string | null>(null);

  useEffect(() => {
    const stored = localStorage.getItem('qwen.adminKey');
    if (stored) {
      setAdminKeyState(stored);
    }
  }, []);

  const setAdminKey = useCallback((key: string) => {
    localStorage.setItem('qwen.adminKey', key);
    setAdminKeyState(key);
  }, []);

  const logout = useCallback(() => {
    localStorage.removeItem('qwen.adminKey');
    setAdminKeyState(null);
  }, []);

  return (
    <AuthContext.Provider value={{ adminKey, setAdminKey, logout }}>
      {children}
    </AuthContext.Provider>
  );
};

export const useAuth = (): AuthContextType => {
  const context = useContext(AuthContext);
  if (!context) throw new Error('useAuth must be used within AuthProvider');
  return context;
};