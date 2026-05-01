import React from 'react';
import { Routes, Route, Navigate } from 'react-router-dom';
import { AuthProvider, useAuth } from './context/AuthContext';
import HealthCard from './components/HealthCard';
import AgentsGrid from './components/AgentsGrid';
import SpecsTable from './components/SpecsTable';
import SpecDetail from './components/SpecDetail';

const AuthGuard: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const { adminKey } = useAuth();
  if (!adminKey) {
    return (
      <div className="auth-overlay">
        <h2>Authentication Required</h2>
        <p>Please enter your Admin API Key to continue.</p>
        <LoginForm />
      </div>
    );
  }
  return <>{children}</>;
};

const LoginForm: React.FC = () => {
  const { setAdminKey } = useAuth();
  const [key, setKey] = React.useState('');

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (key.trim()) {
      setAdminKey(key.trim());
    }
  };

  return (
    <form onSubmit={handleSubmit} className="login-form">
      <input
        type="password"
        value={key}
        onChange={(e) => setKey(e.target.value)}
        placeholder="Enter Admin API Key"
        required
      />
      <button type="submit">Login</button>
    </form>
  );
};

const Overview: React.FC = () => (
  <>
    <HealthCard />
    <AgentsGrid />
  </>
);

const AppContent: React.FC = () => {
  return (
    <div className="app-container">
      <header className="app-header">
        <h1>Qwen Multi-Agent Dashboard</h1>
        <LogoutButton />
      </header>
      <main className="app-main">
        <Routes>
          <Route path="/" element={<AuthGuard><Overview /></AuthGuard>} />
          <Route path="/specs" element={<AuthGuard><SpecsTable /></AuthGuard>} />
          <Route path="/specs/:id" element={<AuthGuard><SpecDetail /></AuthGuard>} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
    </div>
  );
};

const LogoutButton: React.FC = () => {
  const { logout } = useAuth();
  return (
    <button onClick={logout} className="btn-logout">Logout</button>
  );
};

export default function App() {
  return (
    <AuthProvider>
      <AppContent />
    </AuthProvider>
  );
}
